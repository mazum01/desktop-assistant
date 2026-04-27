"""
Thermal service.

Wraps `ThermalManager` (which already has its own poll thread) and
publishes telemetry on the message bus.

Topics published:
    thermal.temp     {"celsius": float, "fahrenheit": float, "ok": bool}
    thermal.fan      {"duty": float}
    thermal.critical {"celsius": float}        — when temp > critical_c
    thermal.error    {"reason": str}           — sensor failure / failsafe

Topics subscribed: (none yet — thermal is autonomous)
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class ThermalService(Service):
    name = "thermal"
    tick_seconds = 1.0

    def __init__(self, bus: Optional[MessageBus] = None, manager=None) -> None:
        super().__init__(bus=bus)
        # Allow injection for tests; default-construct on the Pi.
        self._manager_factory = manager
        self._manager = None
        self._last_critical = False

    def on_start(self) -> None:
        if self._manager is None:
            if self._manager_factory is not None:
                self._manager = self._manager_factory
            else:
                from src.thermal.thermal_manager import ThermalManager
                self._manager = ThermalManager()
        self._manager.start()
        log.info("ThermalService started")

    def run_tick(self) -> None:
        m = self._manager
        if m is None:
            return
        # ThermalManager exposes temperature_c, fan_duty, sensor_ok as
        # @property — never call() them.
        temp_c = m.temperature_c
        ok = m.sensor_ok
        duty = m.fan_duty

        if temp_c is None:
            self.bus.publish("thermal.error", {"reason": "no_reading"})
            return

        self.bus.publish(
            "thermal.temp",
            {"celsius": temp_c, "fahrenheit": temp_c * 9 / 5 + 32, "ok": ok},
        )
        self.bus.publish("thermal.fan", {"duty": duty})

        critical_c = getattr(m, "_thresholds", None)
        if critical_c is not None:
            critical = temp_c >= critical_c.critical_c
            if critical and not self._last_critical:
                self.bus.publish("thermal.critical", {"celsius": temp_c})
            self._last_critical = critical

    def on_stop(self) -> None:
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                log.exception("ThermalManager.stop() failed")
        log.info("ThermalService stopped")
