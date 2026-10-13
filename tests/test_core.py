from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from solar.config import Config
from solar.db import Store
from solar.sems import parse_daily, parse_intraday
from solar.sensors import to_sample
from solar.web import create_app

TZ = ZoneInfo("Europe/Berlin")
DAY = date(2026, 6, 1)


def ts(hour: int, minute: int = 0, day: date = DAY) -> int:
    return int(datetime.combine(day, time(hour, minute), TZ).timestamp())


def sample(pv=1000.0, load=500.0, grid=400.0, battery=-100.0, e_pv=100.0, e_exp=50.0, **kw):
    row = {k: None for k in to_sample({})}
    row.update(pv_w=pv, load_w=load, grid_w=grid, battery_w=battery, soc=50.0,
               e_pv_total=e_pv, e_load_total=200.0, e_export_total=e_exp, e_import_total=10.0,
               e_charge_total=30.0, e_discharge_total=20.0)
    row.update(kw)
    return row


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "t.db"), TZ)
    yield s
    s.close()


def test_to_sample_prefers_meter_and_ignores_non_numeric():
    row = to_sample({"ppv": 171, "meter_active_power_total": -32, "active_power": -33,
                     "battery_soc": 5, "timestamp": datetime.now(), "errors": ""})
    assert row["pv_w"] == 171.0
    assert row["grid_w"] == -32.0  # smart meter wins over the inverter's own estimate
    assert row["soc"] == 5.0
    assert row["load_w"] is None


def test_rollup_5m_splits_import_export(store):
    store.insert_sample(ts(12, 0), sample(grid=600.0, battery=200.0), {})
    store.insert_sample(ts(12, 1), sample(grid=-200.0, battery=-400.0), {})
    store.rollup_5m(ts(12, 0))
    (row,) = store.series_5m(ts(12, 0), ts(12, 5))
    assert row["grid_w"] == pytest.approx(200.0)
    assert row["export_w"] == pytest.approx(300.0)
    assert row["import_w"] == pytest.approx(100.0)
    assert row["discharge_w"] == pytest.approx(100.0)
    assert row["charge_w"] == pytest.approx(200.0)
    assert row["source"] == "local"


def test_daily_uses_counter_delta_with_midnight_baseline(store):
    store.insert_sample(ts(23, 55, date(2026, 5, 31)), sample(e_pv=100.0), {})
    store.insert_sample(ts(6), sample(e_pv=100.5), {})
    store.insert_sample(ts(18), sample(e_pv=130.0, pv=5000.0), {})
    store.insert_sample(ts(19), sample(e_pv=0.0), {})  # garbled frame must be ignored
    store.rollup_daily(DAY)
    (row,) = store.daily_range(DAY, DAY)
    assert row["pv_kwh"] == pytest.approx(30.0)
    assert row["pv_peak_w"] == 5000.0
    assert row["source"] == "local"


def test_partial_local_day_does_not_clobber_import(store):
    store.upsert_daily([{"day": DAY.isoformat(), "pv_kwh": 42.0}], "sems")
    store.insert_sample(ts(15), sample(e_pv=100.0), {})
    store.insert_sample(ts(16), sample(e_pv=101.0), {})
    store.rollup_daily(DAY)
    (row,) = store.daily_range(DAY, DAY)
    assert row["pv_kwh"] == 42.0 and row["source"] == "sems"


def test_import_never_overwrites_local(store):
    store.insert_sample(ts(12), sample(), {})
    store.rollup_5m(ts(12))
    written = store.upsert_5m([{"ts": ts(12), "pv_w": 1.0}], "sems")
    assert written == 0
    assert store.series_5m(ts(12), ts(13))[0]["pv_w"] == 1000.0


def test_parse_daily_skips_all_zero_days():
    rows = parse_daily({
        "proSystemTotalStats": [{"date": "2026-06-01 00:00:00", "val": "31.5"},
                                {"date": "2026-06-02", "val": "0"}],
        "proGridStats": [{"date": "2026-06-01", "val": "12"}, {"date": "2026-06-02", "val": "0"}],
    })
    assert rows == [{"day": "2026-06-01", "pv_kwh": 31.5, "export_kwh": 12.0}]


def test_parse_intraday_scales_and_maps_signs():
    series = {
        "pSystem": [{"tp": "2026-06-01 12:00:00", "power": "2.0"}, {"tp": "2026-06-01 12:01:00", "power": 4}],
        "pBat": [{"tp": "2026-06-01 12:00:00", "power": 1.0}],  # SEMS: + charging
        "pGrid": [{"tp": "2026-06-01 12:00:00", "power": -0.5}],
        "soc": [{"tp": "2026-06-01 12:00:00", "power": 80}],
    }
    (row,) = parse_intraday(series, TZ, scale=1000, grid_sign=1, battery_sign=-1)
    assert row["ts"] == ts(12)
    assert row["pv_w"] == pytest.approx(3000.0)
    assert row["battery_w"] == pytest.approx(-1000.0) and row["charge_w"] == pytest.approx(1000.0)
    assert row["import_w"] == pytest.approx(500.0)
    assert row["soc"] == 80


def test_web_api(tmp_path, monkeypatch):
    db = str(tmp_path / "w.db")
    s = Store(db, TZ)
    now = int(datetime.now(TZ).timestamp())
    s.insert_sample(now, sample(), {"battery_mode_label": "Standby", "secret_unlisted": 1})
    s.rollup_5m(now - 600)
    s.rollup_daily(datetime.now(TZ).date())
    cfg = Config(db_path=db, tz=TZ, inverter_host="", inverter_family=None, poll_interval=10,
                 raw_retention_days=30, metrics_port=0, web_port=0)
    client = TestClient(create_app(cfg, web_dir=str(tmp_path / "none")))

    r = client.get("/api/live")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["pv_w"] == 1000.0 and not body["stale"]
    assert body["status"] == {"battery_mode_label": "Standby"}
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/day").json()["series"]
    assert client.get("/api/days", params={"from": "2026-01-01", "to": "2027-12-31"}).status_code == 400
    assert client.get("/api/periods", params={"by": "week"}).status_code == 422
    assert client.get("/api/summary").json()["lifetime"]["pv_kwh"] == 100.0
