"""Prometheus exposition of every numeric inverter sensor.

Lets an existing Prometheus/Grafana stack scrape the inverter without talking
to it directly. Served by the collector on its own port, separate from the
public web app.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from prometheus_client import CollectorRegistry, Counter, Gauge, start_http_server

_UNIT_SUFFIX = {"W": "watts", "V": "volts", "A": "amperes", "Hz": "hertz", "C": "celsius",
                "kWh": "kwh", "%": "percent", "h": "hours", "VA": "voltamperes", "var": "var"}


def _name(sensor_id: str, unit: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_]", "_", sensor_id).lower()
    suffix = _UNIT_SUFFIX.get(unit)
    return f"goodwe_{base}_{suffix}" if suffix and not base.endswith(suffix) else f"goodwe_{base}"


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.gauges: dict[str, Gauge] = {}
        self.poll_errors = Counter("solar_poll_errors", "Failed inverter polls", registry=self.registry)
        self.last_success = Gauge(
            "solar_last_success_timestamp_seconds", "Unix time of the last good poll", registry=self.registry
        )
        self.info = Gauge("goodwe_inverter_info", "Inverter identity", ["model", "serial", "firmware"],
                          registry=self.registry)

    def set_info(self, model: str, serial: str, firmware: str) -> None:
        self.info.clear()
        self.info.labels(model=model, serial=serial, firmware=firmware).set(1)

    def update(self, sensors: Iterable[Any], data: dict[str, Any], ts: int) -> None:
        for sensor in sensors:
            value = data.get(sensor.id_)
            if not isinstance(value, int | float) or isinstance(value, bool):
                continue
            gauge = self.gauges.get(sensor.id_)
            if gauge is None:
                try:
                    gauge = Gauge(_name(sensor.id_, sensor.unit), sensor.name or sensor.id_,
                                  registry=self.registry)
                except ValueError:  # two sensor ids that normalise to the same metric name
                    continue
                self.gauges[sensor.id_] = gauge
            gauge.set(value)
        self.last_success.set(ts)

    def serve(self, port: int) -> None:
        start_http_server(port, registry=self.registry)
