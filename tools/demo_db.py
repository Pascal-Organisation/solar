"""Generate a synthetic database for local development and screenshots.

    python tools/demo_db.py demo.db
    SOLAR_DB=demo.db SOLAR_WEB_DIR=web python -m solar web
"""

from __future__ import annotations

import math
import random
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

sys.path.insert(0, "src")

from solar.db import Store  # noqa: E402
from solar.sensors import to_sample  # noqa: E402

TZ = ZoneInfo("Europe/Berlin")
RATED = 8000.0


def sun(ts: int) -> float:
    """Clear-sky-ish PV power with seasonal amplitude and passing clouds."""
    local = datetime.fromtimestamp(ts, TZ)
    doy = local.timetuple().tm_yday
    season = 0.55 + 0.45 * math.cos((doy - 172) / 365 * 2 * math.pi)
    day_len = 8 + 8.5 * season
    hour = local.hour + local.minute / 60
    x = (hour - (13.3 - day_len / 2)) / day_len
    if not 0 < x < 1:
        return 0.0
    rnd = random.Random(ts // 1800)
    cloud = 0.35 + 0.65 * rnd.random() ** 0.4
    return RATED * 0.82 * season * math.sin(math.pi * x) ** 1.4 * cloud


def load(ts: int) -> float:
    local = datetime.fromtimestamp(ts, TZ)
    h = local.hour + local.minute / 60
    base = 250 + 180 * math.exp(-((h - 7.5) ** 2) / 1.5) + 900 * math.exp(-((h - 19) ** 2) / 3)
    rnd = random.Random(ts // 300)
    return base + (1800 if rnd.random() < 0.04 else 0) + rnd.uniform(-40, 40)


def main(path: str) -> None:
    store = Store(path, TZ)
    now = int(time.time())
    today = datetime.now(TZ).date()

    # Imported history: daily totals for ~3 years, curves for the last 10 days.
    days = []
    for back in range(1100, 0, -1):
        d = today - timedelta(days=back)
        doy = d.timetuple().tm_yday
        season = 0.55 + 0.45 * math.cos((doy - 172) / 365 * 2 * math.pi)
        rnd = random.Random(d.toordinal())
        pv = max(0.3, 52 * season ** 1.6 * (0.3 + 0.7 * rnd.random()))
        ld = 10 + 4 * (1 - season) + rnd.uniform(-2, 2)
        exp_ = max(0.0, pv - ld * 0.55)
        days.append({"day": d.isoformat(), "pv_kwh": round(pv, 2), "load_kwh": round(ld, 2),
                     "export_kwh": round(exp_, 2), "import_kwh": round(max(0.2, ld * 0.25 - pv * 0.05), 2),
                     "charge_kwh": round(min(9, pv * 0.2), 2), "discharge_kwh": round(min(8, pv * 0.18), 2),
                     "pv_peak_w": round(RATED * 0.85 * season, 0)})
    store.upsert_daily(days, "sems")

    soc = 40.0
    rows = []
    start = int(datetime.combine(today - timedelta(days=10), datetime.min.time(), TZ).timestamp())
    for ts in range(start, now - now % 300 - 86400, 300):
        pv, ld = sun(ts), load(ts)
        bat = max(-3000.0, min(3000.0, ld - pv))
        if (bat < 0 and soc >= 100) or (bat > 0 and soc <= 10):
            bat = 0.0
        soc = max(10.0, min(100.0, soc - bat * (5 / 60) / 100))
        grid = pv + bat - ld
        rows.append({"ts": ts, "pv_w": pv, "load_w": ld, "grid_w": grid, "export_w": max(grid, 0),
                     "import_w": max(-grid, 0), "battery_w": bat, "charge_w": max(-bat, 0),
                     "discharge_w": max(bat, 0), "soc": soc})
    store.upsert_5m(rows, "sems")

    # Locally "measured" last 24 h at 60 s resolution with lifetime counters.
    counters = {"e_total": 27000.0, "e_load_total": 12500.0, "meter_e_total_exp": 18000.0,
                "meter_e_total_imp": 3200.0, "e_bat_charge_total": 5600.0, "e_bat_discharge_total": 4100.0}
    for ts in range(now - 86400, now + 1, 60):
        pv, ld = sun(ts), load(ts)
        bat = max(-3000.0, min(3000.0, ld - pv))
        if (bat < 0 and soc >= 100) or (bat > 0 and soc <= 10):
            bat = 0.0
        soc = max(10.0, min(100.0, soc - bat * (1 / 60) / 100))
        grid = pv + bat - ld
        h = 1 / 60 / 1000
        counters["e_total"] += pv * h
        counters["e_load_total"] += ld * h
        counters["meter_e_total_exp"] += max(grid, 0) * h
        counters["meter_e_total_imp"] += max(-grid, 0) * h
        counters["e_bat_charge_total"] += max(-bat, 0) * h
        counters["e_bat_discharge_total"] += max(bat, 0) * h
        data = {"ppv": round(pv), "ppv1": 0, "ppv2": round(pv), "vpv1": 0.0, "vpv2": 340.0 if pv else 0.0,
                "ipv2": round(pv / 340, 1), "house_consumption": round(ld), "meter_active_power_total": round(grid),
                "meter_active_power1": round(grid / 3), "meter_active_power2": round(grid / 3),
                "meter_active_power3": round(grid / 3), "vgrid": 229.1, "vgrid2": 228.4, "vgrid3": 230.2,
                "meter_freq": 50.0, "total_inverter_power": round(pv + bat), "backup_ptotal": 25,
                "pbattery1": round(bat), "vbattery1": 307.0, "battery_soc": round(soc), "battery_soh": 94,
                "battery_temperature": 21.5, "temperature": 34.0, "battery_modules": 6, "rssi": 40,
                "work_mode_label": "Normal (On-Grid)", "battery_mode_label": "Standby", "errors": "",
                "grid_mode_label": "Connected to grid", "pv1_mode_label": "PV panels not connected",
                "diagnose_result_label": "–", **{k: round(v, 1) for k, v in counters.items()}}
        store.insert_sample(ts, to_sample(data), data)
    store.rollup_5m(now - 86400)
    store.rollup_daily(today - timedelta(days=1))
    store.rollup_daily(today)
    store.set_meta("inverter_model", "GW8K-ET")
    store.set_meta("inverter_rated_power", "8000")
    store.set_meta("sems_import_status", "complete")
    print(f"wrote {path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo.db")
