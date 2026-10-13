"""Read-only HTTP API and static dashboard.

The web process opens the database read-only and never talks to the inverter.
It is the only component reachable from outside (via the Cloudflare tunnel,
behind Cloudflare Access).
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .config import Config
from .db import Store
from .sensors import to_sample

STALE_AFTER = 180
STREAM_INTERVAL = 1
KEEPALIVE_EVERY = 15

# Fields of the full reading worth surfacing as status text on the dashboard.
STATUS_FIELDS = (
    "work_mode_label", "battery_mode_label", "grid_mode_label", "grid_in_out_label",
    "pv1_mode_label", "pv2_mode_label", "errors", "warning_code", "diagnose_result_label",
    "battery_modules", "rssi", "safety_country_label",
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def _ratio(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return max(0.0, min(1.0, part / whole))


def energy_kpis(row: dict[str, Any] | None, price_import: float = 0.0,
                price_export: float = 0.0) -> dict[str, Any] | None:
    """Self-sufficiency (share of load not bought), self-consumption (share of PV kept)
    and, with a tariff, the money PV + battery earned.

    savings = avoided purchase + feed-in revenue
            = (load - import) * price_import + export * price_export
    i.e. the bill without the plant (load * price_import) minus the actual net bill
    (import * price_import - export * price_export).
    """
    if not row:
        return None
    out = dict(row)
    load, imp = row.get("load_kwh"), row.get("import_kwh")
    pv, exp = row.get("pv_kwh"), row.get("export_kwh")
    out["self_sufficiency"] = None if load is None or imp is None else 1 - (_ratio(imp, load) or 0)
    out["self_consumption"] = None if pv is None or exp is None else 1 - (_ratio(exp, pv) or 0)
    out["self_consumed_kwh"] = None if pv is None or exp is None else round(max(0.0, pv - exp), 3)
    if price_import or price_export:
        avoided = None if load is None or imp is None else max(0.0, load - imp) * price_import
        revenue = None if exp is None else exp * price_export
        out["avoided_eur"] = None if avoided is None else round(avoided, 2)
        out["feed_in_eur"] = None if revenue is None else round(revenue, 2)
        out["savings_eur"] = None if avoided is None or revenue is None else round(avoided + revenue, 2)
        out["cost_eur"] = None if imp is None or exp is None else round(imp * price_import - exp * price_export, 2)
    return out


def create_app(cfg: Config, web_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="solar", docs_url=None, redoc_url=None, openapi_url=None)
    local = threading.local()  # sqlite connections must not be shared across threads

    def kpis(row: dict[str, Any] | None) -> dict[str, Any] | None:
        return energy_kpis(row, cfg.price_import, cfg.price_export)

    def store() -> Store:
        s = getattr(local, "store", None)
        if s is None:
            if not os.path.exists(cfg.db_path):
                raise HTTPException(503, "no data yet")
            s = local.store = Store(cfg.db_path, cfg.tz, readonly=True)
        return s

    def today() -> date:
        return datetime.now(cfg.tz).date()

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.update(SECURITY_HEADERS)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    def live_payload(include_all: bool = False) -> dict[str, Any]:
        """The newest reading — refreshed on every poll, not just every stored sample."""
        s = store()
        latest = s.latest()
        if latest is None:
            raise HTTPException(503, "no data yet")
        ts, full = latest
        age = round(time.time() - ts, 1)
        payload = {
            "ts": ts,
            "age": age,
            "stale": age > STALE_AFTER,
            "values": to_sample(full),
            "status": {k: full.get(k) for k in STATUS_FIELDS if k in full},
            "today": kpis(next(iter(s.daily_range(today(), today())), None)),
        }
        if include_all:
            payload["all"] = full
        return payload

    @app.get("/healthz")
    def healthz() -> JSONResponse:
        try:
            payload = live_payload()
        except HTTPException as exc:
            return JSONResponse({"ok": False, "reason": exc.detail}, status_code=503)
        ok = not payload["stale"]
        return JSONResponse({"ok": ok, "age": payload["age"]}, status_code=200 if ok else 503)

    @app.get("/api/live")
    def live() -> dict[str, Any]:
        return live_payload()

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def events() -> AsyncIterator[str]:
            last_ts = None
            idle = 0
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    payload = live_payload(include_all=True)
                except HTTPException:
                    payload = None
                if payload and payload["ts"] != last_ts:
                    last_ts, idle = payload["ts"], 0
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                else:
                    idle += 1
                    if idle % KEEPALIVE_EVERY == 0:
                        yield ": keepalive\n\n"
                await asyncio.sleep(STREAM_INTERVAL)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/day")
    def day(d: date = Query(default=None, alias="date")) -> dict[str, Any]:
        d = d or today()
        s = store()
        start, end = s.day_bounds(d)
        return {
            "date": d.isoformat(),
            "start": start,
            "end": end,
            "series": s.series_5m(start, end),
            "totals": kpis(next(iter(s.daily_range(d, d)), None)),
        }

    @app.get("/api/days")
    def days(
        first: date = Query(alias="from"),
        last: date = Query(alias="to"),
    ) -> dict[str, Any]:
        if last < first or (last - first) > timedelta(days=400):
            raise HTTPException(400, "range must be 0..400 days")
        return {"days": [kpis(r) for r in store().daily_range(first, last)]}

    @app.get("/api/periods")
    def periods(by: str = Query(pattern="^(month|year)$")) -> dict[str, Any]:
        return {"by": by, "periods": [kpis(r) for r in store().grouped(by)]}

    @app.get("/api/summary")
    def summary() -> dict[str, Any]:
        s = store()
        sample = s.latest_sample() or {}
        lifetime = {
            "pv_kwh": sample.get("e_pv_total"),
            "load_kwh": sample.get("e_load_total"),
            "export_kwh": sample.get("e_export_total"),
            "import_kwh": sample.get("e_import_total"),
            "charge_kwh": sample.get("e_charge_total"),
            "discharge_kwh": sample.get("e_discharge_total"),
        }
        first_curve = s.first_curve_ts()
        return {
            "totals": kpis(s.totals()),
            "curves_from": datetime.fromtimestamp(first_curve, cfg.tz).date().isoformat() if first_curve else None,
            "inverter": {
                "model": s.get_meta("inverter_model"),
                "firmware": s.get_meta("inverter_firmware"),
                "rated_power": s.get_meta("inverter_rated_power"),
            },
            "lifetime": kpis(lifetime),
            "tariff": {"import_eur_kwh": cfg.price_import, "export_eur_kwh": cfg.price_export},
            "first_day": s.first_day(),
            "records": s.records(),
            "history_import": s.get_meta("sems_import_status"),
        }

    @app.get("/api/details")
    def details() -> dict[str, Any]:
        latest = store().latest()
        if latest is None:
            raise HTTPException(503, "no data yet")
        ts, full = latest
        raw = store().get_meta("sensors")
        return {"ts": ts, "data": full, "sensors": json.loads(raw) if raw else {}}

    web_dir = web_dir or os.environ.get("SOLAR_WEB_DIR", "/app/web")
    if os.path.isdir(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
