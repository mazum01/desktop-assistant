"""
Thermal service.

Wraps `ThermalManager` (which already has its own poll thread) and
publishes telemetry on the message bus.

Topics published:
    thermal.temp     {"celsius": float, "fahrenheit": float, "ok": bool}
    thermal.fan      {"duty": float, "backend": str, "override": bool, "override_duty": float|null}
    thermal.rpm      {"rpm": int|None, "enabled": bool}
    thermal.critical {"celsius": float}        — when temp > critical_c
    thermal.error    {"reason": str}           — sensor failure / failsafe

Topics subscribed:
    thermal.fan.set_override   {"duty": float}  — pin fan to a fixed duty %
    thermal.fan.clear_override {}               — return to thermal auto mode
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
        self._manager_factory = manager
        self._manager = None
        self._last_critical = False
        self._unsubs: list = []

    def on_start(self) -> None:
        if self._manager is None:
            if self._manager_factory is not None:
                self._manager = self._manager_factory
            else:
                from src.thermal.thermal_manager import ThermalManager
                self._manager = ThermalManager()
        self._manager.start()
        self._unsubs.append(self.bus.subscribe("thermal.fan.set_override",   self._on_set_override))
        self._unsubs.append(self.bus.subscribe("thermal.fan.clear_override", self._on_clear_override))
        log.info("ThermalService started")

    def run_tick(self) -> None:
        m = self._manager
        if m is None:
            return
        temp_c = m.temperature_c  # blended
        ok = m.sensor_ok
        duty = m.fan_duty

        if temp_c is None:
            self.bus.publish("thermal.error", {"reason": "no_reading"})
            return

        self.bus.publish(
            "thermal.temp",
            {
                "celsius":          temp_c,
                "fahrenheit":       temp_c * 9 / 5 + 32,
                "ok":               ok,
                "case_celsius":     getattr(m, "case_temp_c", None),
                "cpu_celsius":      getattr(m, "cpu_temp_c",  None),
                "blended_celsius":  temp_c,
                "case_weight":      getattr(m, "case_weight", 0.2),
                "cpu_weight":       getattr(m, "cpu_weight",  0.8),
            },
        )
        override_on   = getattr(m, "override_active", False)
        override_duty = getattr(m, "override_duty", None)
        self.bus.publish(
            "thermal.fan",
            {
                "duty":          duty,
                "backend":       getattr(m, "fan_backend", "unknown"),
                "override":      override_on,
                "override_duty": override_duty,
            },
        )
        tach_on = getattr(m, "tach_enabled", True)
        self.bus.publish("thermal.rpm", {"rpm": getattr(m, "fan_rpm", None), "enabled": tach_on})

        critical_c = getattr(m, "_thresholds", None)
        if critical_c is not None:
            critical = temp_c >= critical_c.critical_c
            if critical and not self._last_critical:
                self.bus.publish("thermal.critical", {"celsius": temp_c})
            self._last_critical = critical

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._manager is not None:
            try:
                self._manager.stop()
            except Exception:
                log.exception("ThermalManager.stop() failed")
        log.info("ThermalService stopped")

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    def _on_set_override(self, _topic: str, payload) -> None:
        m = self._manager
        if m is None:
            return
        try:
            duty = float(payload.get("duty", 100.0))
        except (TypeError, ValueError):
            log.warning("thermal.fan.set_override: invalid duty %r", payload)
            return
        m.set_override(duty)
        log.info("Fan override set to %.1f%% via bus", duty)

    def _on_clear_override(self, _topic: str, _payload) -> None:
        m = self._manager
        if m is not None:
            m.clear_override()
            log.info("Fan override cleared via bus")
