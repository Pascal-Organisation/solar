"""SQLite storage.

Three tiers, all keyed by unix seconds (UTC):
  samples     every poll (~10 s), pruned after SOLAR_RAW_RETENTION_DAYS
  samples_5m  5-minute averages, kept forever (also the target of the SEMS backfill)
  daily       energy per local calendar day, kept forever

Daily energy is the delta of the inverter's lifetime counters, not an
integral of power samples, so missed polls do not lose energy.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .sensors import COUNTERS, DAY_COUNTERS, SAMPLE_COLUMNS

BUCKET = 300
# A counter baseline taken this long before midnight still counts as "at midnight".
BASELINE_SLACK = 900
# Larger daily deltas than this are a counter glitch, not a sunny day.
MAX_DAILY_KWH = 500.0

FIVE_MIN_COLUMNS = (
    "pv_w", "load_w", "grid_w", "export_w", "import_w",
    "battery_w", "charge_w", "discharge_w", "soc",
)
DAILY_COLUMNS = (*COUNTERS.keys(), "pv_peak_w")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS samples (
    ts INTEGER PRIMARY KEY,
    {", ".join(f"{c} REAL" for c in SAMPLE_COLUMNS)}
);
CREATE TABLE IF NOT EXISTS samples_5m (
    ts INTEGER PRIMARY KEY,
    {", ".join(f"{c} REAL" for c in FIVE_MIN_COLUMNS)},
    n INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily (
    day TEXT PRIMARY KEY,
    {", ".join(f"{c} REAL" for c in DAILY_COLUMNS)},
    coverage REAL NOT NULL DEFAULT 0,
    source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS latest (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ts INTEGER NOT NULL,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path: str, tz: ZoneInfo, readonly: bool = False):
        self.tz = tz
        self.readonly = readonly
        if readonly:
            self.conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
        else:
            self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if not readonly:
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = NORMAL")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        """Add sample columns introduced after a database was created."""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(samples)")}
        with self.conn:
            for column in SAMPLE_COLUMNS:
                if column not in have:
                    self.conn.execute(f"ALTER TABLE samples ADD COLUMN {column} REAL")

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- writes

    def insert_sample(self, ts: int, row: dict[str, float | None], full: dict[str, Any] | None = None) -> None:
        cols = list(SAMPLE_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO samples (ts, {', '.join(cols)}) "
                f"VALUES (?, {', '.join('?' for _ in cols)})",
                [ts, *(row.get(c) for c in cols)],
            )
            if full is not None:
                self._set_latest(ts, full)

    def set_latest(self, ts: float, full: dict[str, Any]) -> None:
        """The newest complete reading; updated on every poll, unlike `samples`."""
        with self.conn:
            self._set_latest(ts, full)

    def _set_latest(self, ts: float, full: dict[str, Any]) -> None:
        self.conn.execute(
            "INSERT INTO latest (id, ts, data) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET ts = excluded.ts, data = excluded.data",
            (ts, json.dumps(full, separators=(",", ":"))),
        )

    def rollup_5m(self, since: int) -> None:
        """(Re)build local 5-minute buckets from raw samples at or after `since`."""
        start = since - since % BUCKET
        with self.conn:
            self.conn.execute(
                f"""
                INSERT INTO samples_5m (ts, {", ".join(FIVE_MIN_COLUMNS)}, n, source)
                SELECT (ts / {BUCKET}) * {BUCKET} AS bucket,
                       avg(pv_w), avg(load_w), avg(grid_w),
                       avg(max(grid_w, 0)), avg(max(-grid_w, 0)),
                       avg(battery_w), avg(max(-battery_w, 0)), avg(max(battery_w, 0)),
                       avg(soc), count(*), 'local'
                FROM samples WHERE ts >= ? GROUP BY bucket
                ON CONFLICT(ts) DO UPDATE SET
                    {", ".join(f"{c} = excluded.{c}" for c in FIVE_MIN_COLUMNS)},
                    n = excluded.n, source = excluded.source
                """,
                (start,),
            )

    def day_bounds(self, day: date) -> tuple[int, int]:
        start = datetime.combine(day, time(), self.tz)
        end = datetime.combine(day + timedelta(days=1), time(), self.tz)
        return int(start.timestamp()), int(end.timestamp())

    def rollup_daily(self, day: date) -> None:
        """Derive one day's energy from lifetime-counter deltas of the raw samples.

        A local row only replaces an existing one if it covers at least as much of
        the day, so a partial first day never clobbers a complete SEMS backfill.
        """
        start, end = self.day_bounds(day)
        counters = list(COUNTERS.values())
        span = self.conn.execute(
            "SELECT min(ts) AS first, max(ts) AS last, max(pv_w) AS peak FROM samples "
            "WHERE ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        if span["first"] is None:
            return
        baseline = self.conn.execute(
            f"SELECT ts, {', '.join(counters)} FROM samples WHERE ts < ? AND ts >= ? "
            "ORDER BY ts DESC LIMIT 1",
            (start, start - BASELINE_SLACK),
        ).fetchone()
        # Without a reading from just before midnight, the lifetime-counter delta
        # misses everything before the first sample. The inverter's own "today"
        # counters start at midnight, so they complete PV, load and battery.
        day_values: dict[str, float] = {}
        before_first: dict[str, float] = {}
        if baseline is None:
            first_dc = self.conn.execute(
                f"SELECT {', '.join(DAY_COUNTERS.values())} FROM samples "
                f"WHERE ts >= ? AND ts < ? AND {DAY_COUNTERS['pv_kwh']} IS NOT NULL ORDER BY ts LIMIT 1",
                (start, end),
            ).fetchone()
            if first_dc is not None:
                for energy_col, counter in DAY_COUNTERS.items():
                    row = self.conn.execute(
                        f"SELECT max({counter}) AS v FROM samples WHERE ts >= ? AND ts < ?", (start, end)
                    ).fetchone()
                    if row["v"] is not None:
                        day_values[energy_col] = round(row["v"], 3)
                    before_first[energy_col] = first_dc[counter] or 0.0
        # Partial days stay "partial" so a complete import can still replace them.
        first_ts = start if baseline is not None else span["first"]
        coverage = min(1.0, (span["last"] - first_ts) / (end - start))

        values: dict[str, float | None] = {}
        for energy_col, counter in COUNTERS.items():
            if energy_col in day_values:
                values[energy_col] = day_values[energy_col]
                continue
            # Counters read as 0 on a garbled frame; ignore those outright.
            agg = self.conn.execute(
                f"SELECT min({counter}) AS lo, max({counter}) AS hi FROM samples "
                f"WHERE ts >= ? AND ts < ? AND {counter} > 0",
                (start, end),
            ).fetchone()
            lo, hi = agg["lo"], agg["hi"]
            if baseline is not None and baseline[counter] and baseline[counter] > 0:
                lo = baseline[counter] if lo is None else min(lo, baseline[counter])
            delta = None if lo is None or hi is None else round(hi - lo, 3)
            values[energy_col] = delta if delta is not None and 0 <= delta <= MAX_DAILY_KWH else None
        if before_first and len(before_first) == len(DAY_COUNTERS):
            # Grid energy before the first sample, by balance:
            # export - import = pv + discharge - charge - load (inverter losses ignored)
            b = before_first
            net = b["pv_kwh"] + b["discharge_kwh"] - b["charge_kwh"] - b["load_kwh"]
            if values.get("export_kwh") is not None and net > 0:
                values["export_kwh"] = round(values["export_kwh"] + net, 3)
            if values.get("import_kwh") is not None and net < 0:
                values["import_kwh"] = round(values["import_kwh"] - net, 3)
        values["pv_peak_w"] = span["peak"]

        cols = list(DAILY_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"""
                INSERT INTO daily (day, {", ".join(cols)}, coverage, source)
                VALUES (?, {", ".join("?" for _ in cols)}, ?, 'local')
                ON CONFLICT(day) DO UPDATE SET
                    {", ".join(f"{c} = excluded.{c}" for c in cols)},
                    coverage = excluded.coverage, source = excluded.source
                WHERE excluded.coverage >= daily.coverage OR daily.source = 'local'
                """,
                [day.isoformat(), *(values[c] for c in cols), coverage],
            )

    def prune(self, now: int, retention_days: int) -> int:
        with self.conn:
            cur = self.conn.execute("DELETE FROM samples WHERE ts < ?", (now - retention_days * 86400,))
        return cur.rowcount

    def upsert_5m(self, rows: Iterable[dict[str, Any]], source: str) -> int:
        """Insert imported buckets; never overwrite locally measured ones."""
        cols = list(FIVE_MIN_COLUMNS)
        count = 0
        with self.conn:
            for r in rows:
                cur = self.conn.execute(
                    f"""
                    INSERT INTO samples_5m (ts, {", ".join(cols)}, n, source)
                    VALUES (?, {", ".join("?" for _ in cols)}, 1, ?)
                    ON CONFLICT(ts) DO UPDATE SET
                        {", ".join(f"{c} = excluded.{c}" for c in cols)}, source = excluded.source
                    WHERE samples_5m.source != 'local'
                    """,
                    [r["ts"], *(r.get(c) for c in cols), source],
                )
                count += cur.rowcount
        return count

    def upsert_daily(self, rows: Iterable[dict[str, Any]], source: str) -> int:
        """Insert imported days; never overwrite locally measured ones."""
        cols = list(DAILY_COLUMNS)
        count = 0
        with self.conn:
            for r in rows:
                cur = self.conn.execute(
                    f"""
                    INSERT INTO daily (day, {", ".join(cols)}, coverage, source)
                    VALUES (?, {", ".join("?" for _ in cols)}, 1.0, ?)
                    ON CONFLICT(day) DO UPDATE SET
                        {", ".join(f"{c} = coalesce(excluded.{c}, daily.{c})" for c in cols)},
                        source = excluded.source
                    WHERE daily.source != 'local'
                    """,
                    [r["day"], *(r.get(c) for c in cols), source],
                )
                count += cur.rowcount
        return count

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # ----------------------------------------------------------------- reads

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def latest(self) -> tuple[int, dict[str, Any]] | None:
        row = self.conn.execute("SELECT ts, data FROM latest WHERE id = 1").fetchone()
        return (row["ts"], json.loads(row["data"])) if row else None

    def latest_sample(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def series_5m(self, start: int, end: int) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT ts, {', '.join(FIVE_MIN_COLUMNS)}, source FROM samples_5m "
            "WHERE ts >= ? AND ts < ? ORDER BY ts",
            (start, end),
        ).fetchall()
        return [dict(r) for r in rows]

    def daily_range(self, first: date, last: date) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            f"SELECT day, {', '.join(DAILY_COLUMNS)}, coverage, source FROM daily "
            "WHERE day >= ? AND day <= ? ORDER BY day",
            (first.isoformat(), last.isoformat()),
        ).fetchall()
        return [dict(r) for r in rows]

    def grouped(self, period: str) -> list[dict[str, Any]]:
        """Energy summed per month ('YYYY-MM') or year ('YYYY')."""
        width = {"month": 7, "year": 4}[period]
        sums = ", ".join(f"round(sum({c}), 3) AS {c}" for c in COUNTERS)
        rows = self.conn.execute(
            f"SELECT substr(day, 1, {width}) AS period, {sums}, count(*) AS days "
            "FROM daily GROUP BY period ORDER BY period"
        ).fetchall()
        return [dict(r) for r in rows]

    def first_curve_ts(self) -> int | None:
        """Oldest 5-minute bucket with PV data (SEMS keeps curves for a limited time only)."""
        row = self.conn.execute("SELECT min(ts) AS t FROM samples_5m WHERE pv_w IS NOT NULL").fetchone()
        return row["t"] if row else None

    def totals(self) -> dict[str, Any]:
        """Energy summed over every stored day."""
        sums = ", ".join(f"round(sum({c}), 3) AS {c}" for c in COUNTERS)
        row = self.conn.execute(f"SELECT {sums}, count(*) AS days, min(day) AS first_day FROM daily").fetchone()
        return dict(row)

    def first_day(self) -> str | None:
        row = self.conn.execute("SELECT min(day) AS d FROM daily").fetchone()
        return row["d"] if row else None

    def records(self) -> dict[str, Any]:
        best = self.conn.execute(
            "SELECT day, pv_kwh FROM daily WHERE pv_kwh IS NOT NULL ORDER BY pv_kwh DESC LIMIT 1"
        ).fetchone()
        peak = self.conn.execute(
            "SELECT day, pv_peak_w FROM daily WHERE pv_peak_w IS NOT NULL ORDER BY pv_peak_w DESC LIMIT 1"
        ).fetchone()
        return {
            "best_day": dict(best) if best else None,
            "peak_power": dict(peak) if peak else None,
        }
