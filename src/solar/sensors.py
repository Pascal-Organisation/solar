"""Map the goodwe library's runtime data onto the columns we persist.

Sign conventions (as reported by GoodWe ET-family inverters and kept as-is):
  grid_w     > 0 exporting to the grid,  < 0 importing from the grid
  battery_w  > 0 discharging,            < 0 charging
So the house balance is  load = pv + battery - grid.
"""

from __future__ import annotations

from typing import Any

# column name -> goodwe sensor id(s); the first id present in the reading wins.
SAMPLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "pv_w": ("ppv",),
    "pv1_w": ("ppv1",),
    "pv2_w": ("ppv2",),
    "pv1_v": ("vpv1",),
    "pv2_v": ("vpv2",),
    "pv1_a": ("ipv1",),
    "pv2_a": ("ipv2",),
    "load_w": ("house_consumption",),
    "grid_w": ("meter_active_power_total", "active_power"),
    "grid_l1_w": ("meter_active_power1", "active_power1"),
    "grid_l2_w": ("meter_active_power2", "active_power2"),
    "grid_l3_w": ("meter_active_power3", "active_power3"),
    "grid_l1_v": ("vgrid",),
    "grid_l2_v": ("vgrid2",),
    "grid_l3_v": ("vgrid3",),
    "grid_hz": ("meter_freq", "fgrid"),
    "inverter_w": ("total_inverter_power",),
    "backup_w": ("backup_ptotal",),
    "battery_w": ("pbattery1",),
    "battery_v": ("vbattery1",),
    "soc": ("battery_soc",),
    "soh": ("battery_soh",),
    "battery_temp": ("battery_temperature",),
    "inverter_temp": ("temperature",),
    # lifetime counters (kWh) — daily energy is derived from their deltas
    "e_pv_total": ("e_total",),
    "e_load_total": ("e_load_total",),
    "e_export_total": ("meter_e_total_exp", "e_total_exp"),
    "e_import_total": ("meter_e_total_imp", "e_total_imp"),
    "e_charge_total": ("e_bat_charge_total",),
    "e_discharge_total": ("e_bat_discharge_total",),
}

# daily energy column -> lifetime counter column it is derived from
COUNTERS: dict[str, str] = {
    "pv_kwh": "e_pv_total",
    "load_kwh": "e_load_total",
    "export_kwh": "e_export_total",
    "import_kwh": "e_import_total",
    "charge_kwh": "e_charge_total",
    "discharge_kwh": "e_discharge_total",
}


def to_sample(data: dict[str, Any]) -> dict[str, float | None]:
    """Pick the persisted columns out of one goodwe reading."""
    row: dict[str, float | None] = {}
    for column, ids in SAMPLE_COLUMNS.items():
        row[column] = None
        for sensor_id in ids:
            value = data.get(sensor_id)
            if isinstance(value, int | float) and not isinstance(value, bool):
                row[column] = float(value)
                break
    return row


def jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Full reading as JSON-safe values (datetimes and enums become strings)."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, bool | int | float | str) or value is None:
            out[key] = value
        else:
            out[key] = str(value)
    return out
