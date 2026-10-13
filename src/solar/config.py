"""Runtime configuration, read from environment variables only.

Nothing site-specific (IP, serial, credentials) lives in the repository; the
deployment provides it via an env file that is root-owned on the host.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from zoneinfo import ZoneInfo


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing required environment variable {name}")
    return value


@dataclass(frozen=True)
class Config:
    db_path: str
    tz: ZoneInfo
    inverter_host: str
    inverter_family: str | None
    poll_interval: float
    raw_retention_days: int
    metrics_port: int
    web_port: int

    @classmethod
    def from_env(cls) -> Config:
        return cls(
            db_path=_env("SOLAR_DB", "/data/solar.db"),
            tz=ZoneInfo(_env("TZ", "Europe/Berlin")),
            inverter_host=_env("SOLAR_INVERTER_HOST", ""),
            inverter_family=os.environ.get("SOLAR_INVERTER_FAMILY") or None,
            poll_interval=float(_env("SOLAR_POLL_INTERVAL", "10")),
            raw_retention_days=int(_env("SOLAR_RAW_RETENTION_DAYS", "30")),
            metrics_port=int(_env("SOLAR_METRICS_PORT", "9108")),
            web_port=int(_env("SOLAR_WEB_PORT", "8080")),
        )
