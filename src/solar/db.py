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

from .sensors import COUNTERS, SAMPLE_COLUMNS

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

    def close(self) -> None:
        self.conn.close()

    # ---------------------------------------------------------------- writes

    def insert_sample(self, ts: int, row: dict[str, float | None], full: dict[str, Any]) -> None:
        cols = list(SAMPLE_COLUMNS)
        with self.conn:
            self.conn.execute(
                f"INSERT OR REPLACE INTO samples (ts, {', '.join(cols)}) "
                f"VALUES (?, {', '.join('?' for _ in cols)})",
                [ts, *(row.get(c) for c in cols)],
            )
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
        first_ts = start if baseline is not None else span["first"]
        coverage = min(1.0, (span["last"] - first_ts) / (end - start))

        values: dict[str, float | None] = {}
        for energy_col, counter in COUNTERS.items():
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
