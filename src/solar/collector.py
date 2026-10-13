"""Poll the inverter over the LAN and persist every reading.

Strictly read-only: only `read_runtime_data()` is ever called. The goodwe
library can also change inverter settings; this program never does.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

import goodwe

from .config import Config
from .db import Store
from .metrics import Metrics
from .sensors import jsonable, to_sample

log = logging.getLogger(__name__)

ROLLUP_EVERY = 60
PRUNE_EVERY = 3600
# After this many failed polls in a row, drop the connection and rediscover.
RECONNECT_AFTER = 6


class Collector:
    def __init__(self, cfg: Config, store: Store, metrics: Metrics):
        self.cfg = cfg
        self.store = store
        self.metrics = metrics
        self.inverter: goodwe.Inverter | None = None
        self.failures = 0
        self.last_rollup = 0.0
        self.last_prune = 0.0
        self.last_day_rolled: str | None = None

    async def connect(self) -> goodwe.Inverter:
        inv = await goodwe.connect(
            self.cfg.inverter_host, family=self.cfg.inverter_family, timeout=2, retries=3
        )
        log.info("connected to %s %s (fw %s)", inv.model_name, inv.serial_number, inv.firmware)
        self.metrics.set_info(inv.model_name or "", inv.serial_number or "", inv.firmware or "")
        self.store.set_meta("inverter_model", inv.model_name or "")
        self.store.set_meta("inverter_firmware", f"{inv.firmware} / {inv.arm_firmware}")
        self.store.set_meta("inverter_rated_power", str(inv.rated_power or ""))
        return inv

    async def poll_once(self) -> bool:
        try:
            if self.inverter is None:
                self.inverter = await self.connect()
            data = await self.inverter.read_runtime_data()
        except Exception as exc:  # the dongle drops UDP frames routinely
            self.failures += 1
            self.metrics.poll_errors.inc()
            log.warning("poll failed (%d in a row): %s", self.failures, exc)
            if self.failures >= RECONNECT_AFTER:
                self.inverter = None
            return False

        self.failures = 0
        now = int(time.time())
        self.store.insert_sample(now, to_sample(data), jsonable(data))
        self.metrics.update(self.inverter.sensors(), data, now)
        return True

    def housekeeping(self, now: float) -> None:
        if now - self.last_rollup >= ROLLUP_EVERY:
            self.last_rollup = now
            self.store.rollup_5m(int(now) - 2 * 300)
            today = datetime.fromtimestamp(now, self.cfg.tz).date()
            # Also close out yesterday once after midnight with its final counters.
            if self.last_day_rolled not in (None, today.isoformat()):
                self.store.rollup_daily(datetime.fromisoformat(self.last_day_rolled).date())
            self.store.rollup_daily(today)
            self.last_day_rolled = today.isoformat()
        if now - self.last_prune >= PRUNE_EVERY:
            self.last_prune = now
            removed = self.store.prune(int(now), self.cfg.raw_retention_days)
            if removed:
                log.info("pruned %d raw samples", removed)

    async def run(self) -> None:
        if not self.cfg.inverter_host:
            raise SystemExit("SOLAR_INVERTER_HOST is not set")
        log.info("polling %s every %ss", self.cfg.inverter_host, self.cfg.poll_interval)
        while True:
            started = time.monotonic()
            await self.poll_once()
            try:
                self.housekeeping(time.time())
            except Exception:
                log.exception("housekeeping failed")
            elapsed = time.monotonic() - started
            await asyncio.sleep(max(0.5, self.cfg.poll_interval - elapsed))
