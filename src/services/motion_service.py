"""
Motion service.

Wraps `ServoController` and exposes pan commands over the message bus.

Topics subscribed:
    motion.pan_to     {"angle": float}            — move pan servo to logical angle
    motion.relax      None                        — release torque
    motion.stop       None                        — stop any in-progress sweep

Topics published:
    motion.position   {"angle": float}            — every tick
    motion.moved      {"from": float, "to": float, "direction": str}
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class MotionService(Service):
    name = "motion"
    tick_seconds = 0.5

    def __init__(self, bus: Optional[MessageBus] = None, controller=None) -> None:
        super().__init__(bus=bus)
        self._controller = controller
        self._unsubs = []

    def on_start(self) -> None:
        if self._controller is None:
            from src.motion.servo_controller import ServoController
            self._controller = ServoController()

        self._unsubs.append(self.bus.subscribe("motion.pan_to", self._on_pan_to))
        self._unsubs.append(self.bus.subscribe("motion.relax", self._on_relax))
        self._unsubs.append(self.bus.subscribe("motion.stop", self._on_stop_cmd))
        log.info("MotionService started; hardware_ready=%s",
                 getattr(self._controller, "hardware_ready", False))

    def run_tick(self) -> None:
        if self._controller is None:
            return
        try:
            pos = self._controller.position
            self.bus.publish("motion.position", {"angle": float(pos)})
        except Exception:
            log.exception("position read failed")

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._controller is not None:
            try:
                self._controller.stop()
                self._controller.relax()
            except Exception:
                log.exception("controller stop/relax failed")
        log.info("MotionService stopped")

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_pan_to(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict) or "angle" not in payload:
            log.warning("motion.pan_to ignored: bad payload %r", payload)
            return
        angle = float(payload["angle"])
        before = float(self._controller.position)
        try:
            direction = self._controller.plan_direction(before, angle)
        except Exception:
            direction = "?"
        try:
            self._controller.move_to(angle)
        except Exception:
            log.exception("move_to(%s) failed", angle)
            return
        self.bus.publish(
            "motion.moved",
            {"from": before, "to": angle, "direction": direction},
        )

    def _on_relax(self, _topic: str, _payload) -> None:
        try:
            self._controller.relax()
        except Exception:
            log.exception("relax failed")

    def _on_stop_cmd(self, _topic: str, _payload) -> None:
        try:
            self._controller.stop()
        except Exception:
            log.exception("stop failed")
