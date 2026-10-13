"""Command line entry point: `solar <command>`."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import date

from .config import Config


def _collect(cfg: Config, _: argparse.Namespace) -> None:
    from .collector import Collector
    from .db import Store
    from .metrics import Metrics

    metrics = Metrics()
    metrics.serve(cfg.metrics_port)
    asyncio.run(Collector(cfg, Store(cfg.db_path, cfg.tz), metrics).run())


def _web(cfg: Config, _: argparse.Namespace) -> None:
    import uvicorn

    from .web import create_app

    uvicorn.run(create_app(cfg), host="0.0.0.0", port=cfg.web_port,  # noqa: S104 - container-internal
                proxy_headers=False, server_header=False, access_log=False)


def _sems_client():
    from .sems import SemsClient

    account, password = os.environ.get("SEMS_ACCOUNT"), os.environ.get("SEMS_PASSWORD")
    if not account or not password:
        raise SystemExit("set SEMS_ACCOUNT and SEMS_PASSWORD")
    return SemsClient(account, password, pace=float(os.environ.get("SEMS_PACE", "5")))


def _sems_import(cfg: Config, args: argparse.Namespace) -> None:
    from .db import Store
    from .sems import Importer

    Importer(Store(cfg.db_path, cfg.tz), _sems_client(), cfg.tz).run(intraday=not args.daily_only)


def _sems_probe(cfg: Config, args: argparse.Namespace) -> None:
    from .db import Store
    from .sems import Importer

    result = Importer(Store(cfg.db_path, cfg.tz), _sems_client(), cfg.tz).probe(args.date)
    print(json.dumps(result, indent=2, default=str))


def _healthcheck(cfg: Config, args: argparse.Namespace) -> None:
    """Container HEALTHCHECK: web checks its own endpoint, collector checks data freshness."""
    if args.target == "web":
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{cfg.web_port}/healthz", timeout=5) as r:  # noqa: S310
                sys.exit(0 if r.status == 200 else 1)
        except Exception:
            sys.exit(1)
    from .db import Store

    try:
        sample = Store(cfg.db_path, cfg.tz, readonly=True).latest_sample()
    except Exception:
        sys.exit(1)
    sys.exit(0 if sample and time.time() - sample["ts"] < 180 else 1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="solar")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("collect", help="poll the inverter and store readings")
    sub.add_parser("web", help="serve the dashboard and read-only API")
    imp = sub.add_parser("sems-import", help="backfill history from the SEMS portal (resumable)")
    imp.add_argument("--daily-only", action="store_true", help="skip the 5-minute curves")
    probe = sub.add_parser("sems-probe", help="fetch one SEMS day and compare it with local data")
    probe.add_argument("--date", type=date.fromisoformat, required=True)
    hc = sub.add_parser("healthcheck")
    hc.add_argument("target", choices=["web", "collector"])
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    commands = {"collect": _collect, "web": _web, "sems-import": _sems_import,
                "sems-probe": _sems_probe, "healthcheck": _healthcheck}
    commands[args.command](Config.from_env(), args)


if __name__ == "__main__":
    main()
