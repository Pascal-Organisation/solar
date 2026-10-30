from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from solar.config import Config
from solar.db import Store
from solar.sems import parse_daily, parse_intraday, password_hash
from solar.sensors import to_sample
from solar.web import create_app, energy_kpis

TZ = ZoneInfo("Europe/Berlin")
DAY = date(2026, 6, 1)


def ts(hour: int, minute: int = 0, day: date = DAY) -> int:
    return int(datetime.combine(day, time(hour, minute), TZ).timestamp())


def sample(pv=1000.0, load=500.0, grid=400.0, battery=-100.0, e_pv=100.0, e_exp=50.0, e_imp=10.0, **kw):
    row = {k: None for k in to_sample({})}
    row.update(pv_w=pv, load_w=load, grid_w=grid, battery_w=battery, soc=50.0,
               e_pv_total=e_pv, e_load_total=200.0, e_export_total=e_exp, e_import_total=e_imp,
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


def test_first_day_completed_from_inverter_day_counters(store):
    # Collector started at 10:00 - no midnight baseline. The lifetime delta would
    # only see 10:00-18:00; the inverter's "today" counters know the whole day.
    # Before 10:00: pv 1, load 4, charge 0, discharge 1 -> net -2 kWh = imported.
    store.insert_sample(ts(10), sample(e_pv=100.0, e_imp=10.0, e_pv_day=1.0, e_load_day=4.0,
                                       e_charge_day=0.0, e_discharge_day=1.0), {})
    store.insert_sample(ts(18), sample(e_pv=110.0, e_imp=10.5, e_pv_day=11.0, e_load_day=9.5,
                                       e_charge_day=2.0, e_discharge_day=1.0), {})
    store.rollup_daily(DAY)
    (row,) = store.daily_range(DAY, DAY)
    assert row["pv_kwh"] == pytest.approx(11.0)
    assert row["load_kwh"] == pytest.approx(9.5)
    assert row["import_kwh"] == pytest.approx(0.5 + 2.0)  # measured window + balance before it
    assert row["coverage"] == pytest.approx(8 / 24, abs=0.01)  # still partial: import may replace it


def test_migration_adds_new_sample_columns(tmp_path):
    import sqlite3

    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE samples (ts INTEGER PRIMARY KEY, pv_w REAL)")
    old.commit()
    old.close()
    s = Store(path, TZ)
    cols = {r["name"] for r in s.conn.execute("PRAGMA table_info(samples)")}
    assert {"e_pv_day", "e_discharge_day", "soc"} <= cols


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
        "pBat": [{"tp": "2026-06-01 12:00:00", "power": 1.0}],  # flipped by battery_sign=-1
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
    # A newer reading than the last stored sample: live values must come from it.
    s.set_latest(now + 1.5, {"ppv": 1234, "battery_soc": 51})
    cfg = Config(db_path=db, tz=TZ, inverter_host="", inverter_family=None, poll_interval=2,
                 store_interval=10, raw_retention_days=30, metrics_port=0, web_port=0,
                 price_import=0.33, price_export=0.08)
    client = TestClient(create_app(cfg, web_dir=str(tmp_path / "none")))

    r = client.get("/api/live")
    assert r.status_code == 200
    body = r.json()
    assert body["values"]["pv_w"] == 1234.0 and body["values"]["soc"] == 51.0 and not body["stale"]
    assert "all" not in body
    assert body["lifetime"] is None  # the newest reading carries no counters
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["cache-control"] == "no-store"

    assert client.get("/healthz").status_code == 200
    assert client.get("/api/day").json()["series"]
    assert client.get("/api/days", params={"from": "2026-01-01", "to": "2027-12-31"}).status_code == 400
    assert client.get("/api/periods", params={"by": "week"}).status_code == 422
    summary = client.get("/api/summary").json()
    assert summary["lifetime"]["pv_kwh"] == 100.0
    assert summary["tariff"] == {"import_eur_kwh": 0.33, "export_eur_kwh": 0.08}
    assert summary["totals"]["days"] == 1 and "savings_eur" in summary["totals"]
    assert summary["curves_from"] == datetime.now(TZ).date().isoformat()
    assert "savings_eur" in client.get("/api/day").json()["totals"]

    # With counters in the newest reading, the all-time balance comes live with it.
    s.set_latest(now + 3, {"e_total": 101.0, "e_load_total": 201.0, "meter_e_total_exp": 51.0,
                           "meter_e_total_imp": 10.0, "e_bat_charge_total": 30.0, "e_bat_discharge_total": 21.0})
    live = client.get("/api/live").json()["lifetime"]
    assert live["pv_kwh"] == 101.0 and live["savings_eur"] > summary["lifetime"]["savings_eur"]
    # A garbled frame with a zeroed counter must not make the total dip.
    s.set_latest(now + 4, {"e_total": 101.0, "e_load_total": 0.0, "meter_e_total_exp": 51.0,
                           "meter_e_total_imp": 10.0, "e_bat_charge_total": 30.0, "e_bat_discharge_total": 21.0})
    assert client.get("/api/live").json()["lifetime"] is None


def test_savings_formula():
    k = energy_kpis({"pv_kwh": 30.0, "load_kwh": 15.0, "import_kwh": 3.0, "export_kwh": 16.0}, 0.33, 0.08)
    assert k["avoided_eur"] == pytest.approx(12 * 0.33, abs=0.01)   # 12 kWh not bought
    assert k["feed_in_eur"] == pytest.approx(16 * 0.08, abs=0.01)
    assert k["savings_eur"] == pytest.approx(3.96 + 1.28, abs=0.01)
    # savings == bill without plant - actual net bill
    assert k["savings_eur"] == pytest.approx(15 * 0.33 - k["cost_eur"], abs=0.01)
    assert k["self_consumed_kwh"] == 14.0
    assert "savings_eur" not in energy_kpis({"load_kwh": 1.0, "import_kwh": 1.0})


def test_battery_kpis():
    k = energy_kpis({"load_kwh": 100.0, "import_kwh": 20.0, "charge_kwh": 50.0, "discharge_kwh": 40.0}, 0.33, 0.08)
    assert k["battery_efficiency"] == pytest.approx(0.8)
    assert k["battery_loss_kwh"] == pytest.approx(10.0)
    assert k["battery_share"] == pytest.approx(0.4)
    # 40 kWh replaced grid power at 33 ct, 50 kWh of PV surplus forwent 8 ct each.
    assert k["battery_eur"] == pytest.approx(40 * 0.33 - 50 * 0.08)
    # A day with barely any charging says nothing about efficiency.
    assert energy_kpis({"charge_kwh": 0.4, "discharge_kwh": 3.0})["battery_efficiency"] is None
    assert "battery_eur" not in energy_kpis({"charge_kwh": 5.0, "discharge_kwh": 4.0})


def test_password_hash_matches_sems_wire_format():
    # base64(md5hex("secret"))
    assert password_hash("secret") == "NWViZTIyOTRlY2QwZTBmMDhlYWI3NjkwZDJhNmVlNjk="
