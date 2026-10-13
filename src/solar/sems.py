"""One-off backfill of historical data from GoodWe's SEMS portal (SEMS+ gateway API).

The SEMS API is undocumented; endpoints and payloads follow the open-source
Home Assistant / ioBroker integrations (see docs/sems-api.md). Because units and
sign conventions are not documented, run `solar sems-probe --date <day>` for a
day that was also measured locally before running the full import.

Credentials come from SEMS_ACCOUNT / SEMS_PASSWORD and are never stored.
Imported rows never overwrite locally measured ones.
"""

from __future__ import annotations

import base64
import calendar
import hashlib
import json
import logging
import os
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .db import BUCKET, Store

log = logging.getLogger(__name__)

LOGIN_URL = "https://eu-semsplus.goodwe.com/web/sems/sems-user/api/v1/auth/cross-login"
DEFAULT_API = "https://eu-gateway.semsportal.com/web/sems"
ORIGIN = "https://eu-semsplus.goodwe.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
RELOGIN_CODES = {"100002", "C0602"}
RATE_LIMIT_CODES = {"GY0429"}
INTRADAY_ITEMS = ["pSystem", "soc", "pBat", "pConsum", "pGrid"]
DAILY_ITEMS = {
    "proSystemTotalStats": "pv_kwh",
    "proConsumStats": "load_kwh",
    "proGridStats": "export_kwh",
    "proPurchaseStats": "import_kwh",
    "proCharStats": "charge_kwh",
    "proDischarStats": "discharge_kwh",
}


class SemsError(RuntimeError):
    pass


def password_hash(password: str) -> str:
    return base64.b64encode(hashlib.md5(password.encode()).hexdigest().encode()).decode()  # noqa: S324


def signature(uid: str, token: str) -> str:
    ts = round(time.time() * 1000)
    digest = hashlib.sha256(f"{ts}@{uid}@{token}".encode()).hexdigest()
    return base64.b64encode(f"{digest}@{ts}".encode()).decode()


class SemsClient:
    def __init__(self, account: str, password: str, pace: float = 5.0):
        self.account = account
        self.password = password
        self.pace = pace
        self.session: dict[str, Any] | None = None
        self.api = DEFAULT_API
        self.http = httpx.Client(timeout=30, headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "Origin": ORIGIN,
            "Referer": ORIGIN + "/",
            "User-Agent": USER_AGENT,
        })
        self._last_call = 0.0

    def _throttle(self) -> None:
        wait = self.pace - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.monotonic()

    def login(self) -> None:
        self._throttle()
        anon = {"uid": "", "timestamp": 0, "token": "", "client": "semsPlusWeb",
                "version": "", "language": "en"}
        r = self.http.post(LOGIN_URL, headers={"token": json.dumps(anon), "X-Signature": signature("", "")},
                           json={"account": self.account, "pwd": password_hash(self.password),
                                 "agreement": 1, "isChinese": False, "isLocal": False})
        r.raise_for_status()
        body = r.json()
        if str(body.get("code")) not in ("00000", "0") or not body.get("data"):
            raise SemsError(f"login failed: code={body.get('code')} msg={body.get('msg') or body.get('message')}")
        self.session = body["data"]
        self.api = (self.session.get("api") or DEFAULT_API).rstrip("/")
        log.info("logged in to SEMS (region %s, api %s)", self.session.get("region"), self.api)

    def call(self, path: str, payload: dict[str, Any] | None = None, *, _retry: bool = True) -> Any:
        if self.session is None:
            self.login()
        assert self.session is not None
        for attempt in range(6):
            self._throttle()
            headers = {
                "token": json.dumps({**self.session, "client": "semsPlusWeb", "language": "en"}),
                "X-Signature": signature(str(self.session.get("uid", "")), str(self.session.get("token", ""))),
            }
            try:
                r = self.http.post(f"{self.api}{path}", headers=headers, json=payload or {})
            except httpx.TransportError as exc:
                log.warning("transport error on %s: %s (attempt %d)", path, exc, attempt + 1)
                time.sleep(min(300, 10 * 2**attempt))
                continue
            if r.status_code == 429 or r.status_code >= 500:
                delay = int(r.headers.get("Retry-After", 0)) or min(900, 30 * 2**attempt)
                log.warning("HTTP %s on %s, sleeping %ss", r.status_code, path, delay)
                time.sleep(max(delay, 300 if r.status_code == 429 else delay))
                continue
            r.raise_for_status()
            body = r.json()
            code = str(body.get("code"))
            if code in ("00000", "0"):
                return body.get("data")
            if code in RATE_LIMIT_CODES:
                log.warning("rate limited (%s), sleeping 300s", code)
                time.sleep(300 * (attempt + 1))
                continue
            if code in RELOGIN_CODES and _retry:
                log.info("session expired (%s), logging in again", code)
                self.login()
                return self.call(path, payload, _retry=False)
            raise SemsError(f"{path}: code={code} msg={body.get('msg') or body.get('message')}")
        raise SemsError(f"{path}: giving up after retries")

    # ------------------------------------------------------------- endpoints

    def station_ids(self) -> list[dict[str, Any]]:
        data = self.call("/sems-plant/api/portal/stations/page", {"current": 1, "size": 100})
        return [{"id": s.get("id"), "name": s.get("stationName") or s.get("name")}
                for s in (data or {}).get("dataList", [])]

    def statistics(self, station: str, dimension: str, start: datetime, end: datetime) -> dict[str, list]:
        data = self.call("/sems-plant/api/stations/statistics", {
            "stationId": station, "isReport": False, "items": list(DAILY_ITEMS),
            "dimension": dimension,
            "startTime": start.strftime("%Y-%m-%d %H:%M:%S"),
            "endTime": end.strftime("%Y-%m-%d %H:%M:%S"),
        })
        return {d.get("item"): d.get("statisticsList") or [] for d in (data or {}).get("dataList", [])}

    def intraday(self, station: str, day: date, tz: ZoneInfo) -> dict[str, list]:
        offset = datetime.combine(day, datetime.min.time().replace(hour=12), tz).utcoffset()
        data = self.call("/sems-plant/api/v1/hems/power/statisticsAndPreV2", {
            "stationId": station, "items": INTRADAY_ITEMS, "timeScale": 1,
            "timeZone": int(offset.total_seconds() // 3600) if offset else 0,
            "startTime": f"{day.isoformat()} 00:00:00",
            "endTime": f"{day.isoformat()} 23:59:59",
        })
        return {d.get("item"): d.get("powerData") or [] for d in (data or {}).get("dataList", [])}


# ------------------------------------------------------------------ parsing

def _num(value: Any) -> float | None:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def parse_daily(stats: dict[str, list]) -> list[dict[str, Any]]:
    by_day: dict[str, dict[str, Any]] = defaultdict(dict)
    for item, column in DAILY_ITEMS.items():
        for point in stats.get(item, []):
            day = str(point.get("date", ""))[:10]
            value = _num(point.get("val"))
            if len(day) == 10 and value is not None:
                by_day[day][column] = value
    rows = []
    for day, values in sorted(by_day.items()):
        if any(v for v in values.values()):  # all-zero days are "no data", not "no sun"
            rows.append({"day": day, **values})
    return rows


def parse_intraday(series: dict[str, list], tz: ZoneInfo, scale: float, grid_sign: float,
                   battery_sign: float) -> list[dict[str, Any]]:
    """Convert SEMS curves to 5-minute buckets in our sign convention.

    `scale` converts SEMS units to W; the signs map SEMS conventions onto ours
    (grid: + export, battery: + discharge). SEMS+ already uses that convention,
    so both default to 1; they exist for accounts/firmware that differ.
    """
    points: dict[int, dict[str, float]] = defaultdict(dict)
    names = {"pSystem": "pv_w", "pConsum": "load_w", "pGrid": "grid_w", "pBat": "battery_w", "soc": "soc"}
    for item, column in names.items():
        for p in series.get(item, []):
            value = _num(p.get("power"))
            try:
                local = datetime.strptime(str(p.get("tp")), "%Y-%m-%d %H:%M:%S").replace(tzinfo=tz)
            except ValueError:
                continue
            if value is None:
                continue
            if column == "grid_w":
                value *= scale * grid_sign
            elif column == "battery_w":
                value *= scale * battery_sign
            elif column != "soc":
                value *= scale
            points[int(local.timestamp())][column] = value

    buckets: dict[int, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for ts, values in points.items():
        b = buckets[ts - ts % BUCKET]
        for column, value in values.items():
            b[column].append(value)
            if column == "grid_w":
                b["export_w"].append(max(value, 0.0))
                b["import_w"].append(max(-value, 0.0))
            elif column == "battery_w":
                b["discharge_w"].append(max(value, 0.0))
                b["charge_w"].append(max(-value, 0.0))
    rows = []
    for ts in sorted(buckets):
        row: dict[str, Any] = {"ts": ts}
        for column, values in buckets[ts].items():
            row[column] = sum(values) / len(values)
        rows.append(row)
    return rows


# --------------------------------------------------------------- importer

class Importer:
    def __init__(self, store: Store, client: SemsClient, tz: ZoneInfo):
        self.store = store
        self.client = client
        self.tz = tz
        self.scale = float(os.environ.get("SEMS_POWER_SCALE", "0")) or None
        self.grid_sign = float(os.environ.get("SEMS_GRID_SIGN", "1"))
        self.battery_sign = float(os.environ.get("SEMS_BATTERY_SIGN", "1"))

    def station(self) -> str:
        configured = os.environ.get("SEMS_STATION_ID")
        if configured:
            return configured
        stations = self.client.station_ids()
        if len(stations) != 1:
            raise SemsError(f"found {len(stations)} stations, set SEMS_STATION_ID: {stations}")
        return str(stations[0]["id"])

    def find_start_year(self, station: str) -> int:
        cached = self.store.get_meta("sems_start_year")
        if cached:
            return int(cached)
        this_year = datetime.now(self.tz).year
        first = this_year
        for year in range(this_year, 2014, -1):
            stats = self.client.statistics(station, "year", datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59))
            total = sum(_num(p.get("val")) or 0 for p in stats.get("proSystemTotalStats", []))
            log.info("year %d: %.1f kWh PV", year, total)
            if total > 0:
                first = year
            elif year < this_year:
                break
        self.store.set_meta("sems_start_year", str(first))
        return first

    def import_daily(self, station: str, start_year: int) -> date | None:
        """Daily totals, one calendar month per request. Returns the first day with data."""
        first_day: date | None = None
        today = datetime.now(self.tz).date()
        cursor = self.store.get_meta("sems_daily_cursor")
        year, month = (int(cursor[:4]), int(cursor[5:7])) if cursor else (start_year, 1)
        while (year, month) <= (today.year, today.month):
            last = calendar.monthrange(year, month)[1]
            stats = self.client.statistics(station, "day", datetime(year, month, 1),
                                           datetime(year, month, last, 23, 59, 59))
            rows = [r for r in parse_daily(stats) if r["day"] < today.isoformat()]
            written = self.store.upsert_daily(rows, "sems")
            if rows and first_day is None:
                first_day = date.fromisoformat(rows[0]["day"])
            log.info("daily %04d-%02d: %d days, %d written", year, month, len(rows), written)
            self.store.set_meta("sems_daily_cursor", f"{year:04d}-{month:02d}")
            self.store.set_meta("sems_import_status", f"daily totals up to {year:04d}-{month:02d}")
            year, month = (year + 1, 1) if month == 12 else (year, month + 1)
        return first_day or (date.fromisoformat(self.store.first_day()) if self.store.first_day() else None)

    def detect_scale(self, series: dict[str, list]) -> float:
        if self.scale:
            return self.scale
        peak = max((abs(_num(p.get("power")) or 0) for p in series.get("pSystem", [])), default=0)
        # An 8 kW plant reporting single-digit peaks is reporting kW.
        self.scale = 1000.0 if 0 < peak < 50 else 1.0
        log.info("SEMS power unit detected as %s", "kW" if self.scale == 1000 else "W")
        return self.scale

    def import_intraday(self, station: str, first_day: date) -> None:
        cursor = self.store.get_meta("sems_intraday_cursor")
        day = date.fromisoformat(cursor) + timedelta(days=1) if cursor else first_day
        today = datetime.now(self.tz).date()
        while day < today:
            series = self.client.intraday(station, day, self.tz)
            if any(series.values()):
                rows = parse_intraday(series, self.tz, self.detect_scale(series), self.grid_sign,
                                      self.battery_sign)
                written = self.store.upsert_5m(rows, "sems")
                log.info("intraday %s: %d buckets, %d written", day, len(rows), written)
            else:
                log.info("intraday %s: no data", day)
            self.store.set_meta("sems_intraday_cursor", day.isoformat())
            self.store.set_meta("sems_import_status", f"curves up to {day.isoformat()}")
            day += timedelta(days=1)

    def run(self, intraday: bool = True) -> None:
        station = self.station()
        start_year = self.find_start_year(station)
        log.info("station %s, data from %d", station, start_year)
        first_day = self.import_daily(station, start_year)
        if intraday and first_day:
            self.import_intraday(station, first_day)
        self.store.set_meta("sems_import_status", "complete")
        log.info("SEMS import complete")

    def probe(self, day: date) -> dict[str, Any]:
        """Fetch one day and compare it with local data for the same day, if any."""
        station = self.station()
        series = self.client.intraday(station, day, self.tz)
        start, end = self.store.day_bounds(day)
        local = {(r["ts"]): r for r in self.store.series_5m(start, end) if r["source"] == "local"}
        stats = parse_daily(self.client.statistics(station, "day", datetime.combine(day, datetime.min.time()),
                                                   datetime.combine(day, datetime.max.time()).replace(microsecond=0)))
        summary: dict[str, Any] = {"station": station, "date": day.isoformat(), "daily_stats": stats, "items": {}}
        for item, pts in series.items():
            vals = [v for v in (_num(p.get("power")) for p in pts) if v is not None]
            summary["items"][item] = {
                "points": len(vals), "min": min(vals, default=None), "max": max(vals, default=None),
                "first": pts[:2], "sum_kwh_if_kw_per_min": round(sum(vals) / 60, 2) if vals else None,
            }
        if local:
            rows = parse_intraday(series, self.tz, self.detect_scale(series), self.grid_sign, self.battery_sign)
            pairs = [(r, local[r["ts"]]) for r in rows if r["ts"] in local]
            for column in ("pv_w", "load_w", "grid_w", "battery_w", "soc"):
                xs = [(a.get(column), b.get(column)) for a, b in pairs
                      if a.get(column) is not None and b.get(column) is not None]
                if xs:
                    agree = sum(1 for a, b in xs if abs(a) < 50 or abs(b) < 50 or (a > 0) == (b > 0))
                    mean_abs_diff = sum(abs(a - b) for a, b in xs) / len(xs)
                    summary.setdefault("vs_local", {})[column] = {
                        "buckets": len(xs), "sign_agreement": round(agree / len(xs), 3),
                        "mean_abs_diff": round(mean_abs_diff, 1),
                    }
        return summary
