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

STALE_AFTER = 180
STREAM_INTERVAL = 5

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


def energy_kpis(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Self-sufficiency (share of load not bought) and self-consumption (share of PV kept)."""
    if not row:
        return None
    out = dict(row)
    load, imp = row.get("load_kwh"), row.get("import_kwh")
    pv, exp = row.get("pv_kwh"), row.get("export_kwh")
    out["self_sufficiency"] = None if load is None or imp is None else 1 - (_ratio(imp, load) or 0)
    out["self_consumption"] = None if pv is None or exp is None else 1 - (_ratio(exp, pv) or 0)
    return out


def create_app(cfg: Config, web_dir: str | None = None) -> FastAPI:
    app = FastAPI(title="solar", docs_url=None, redoc_url=None, openapi_url=None)
    local = threading.local()  # sqlite connections must not be shared across threads

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

    def live_payload() -> dict[str, Any]:
        s = store()
        sample = s.latest_sample()
        latest = s.latest()
        if sample is None or latest is None:
            raise HTTPException(503, "no data yet")
        _, full = latest
        age = int(time.time()) - sample["ts"]
        return {
            "ts": sample["ts"],
            "age": age,
            "stale": age > STALE_AFTER,
            "values": {k: v for k, v in sample.items() if k != "ts"},
            "status": {k: full.get(k) for k in STATUS_FIELDS if k in full},
            "today": energy_kpis(next(iter(s.daily_range(today(), today())), None)),
        }

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
            yield "retry: 5000\n\n"
            while not await request.is_disconnected():
                try:
                    payload = live_payload()
                except HTTPException:
                    payload = None
                if payload and payload["ts"] != last_ts:
                    last_ts = payload["ts"]
                    yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
                else:
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
            "totals": energy_kpis(next(iter(s.daily_range(d, d)), None)),
        }

    @app.get("/api/days")
    def days(
        first: date = Query(alias="from"),
        last: date = Query(alias="to"),
    ) -> dict[str, Any]:
        if last < first or (last - first) > timedelta(days=400):
            raise HTTPException(400, "range must be 0..400 days")
        return {"days": [energy_kpis(r) for r in store().daily_range(first, last)]}

    @app.get("/api/periods")
    def periods(by: str = Query(pattern="^(month|year)$")) -> dict[str, Any]:
        return {"by": by, "periods": [energy_kpis(r) for r in store().grouped(by)]}

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
        return {
            "inverter": {
                "model": s.get_meta("inverter_model"),
                "firmware": s.get_meta("inverter_firmware"),
                "rated_power": s.get_meta("inverter_rated_power"),
            },
            "lifetime": energy_kpis(lifetime),
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
        return {"ts": ts, "data": full}

    web_dir = web_dir or os.environ.get("SOLAR_WEB_DIR", "/app/web")
    if os.path.isdir(web_dir):
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")
    return app
