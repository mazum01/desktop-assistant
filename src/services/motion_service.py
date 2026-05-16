"""
Motion service.

Wraps `ServoController` and exposes pan commands over the message bus.

Topics subscribed:
    motion.pan_to         {"angle": float, "move_time_ms"?: float}
    motion.relax          None
    motion.stop           None
    motion.set_enabled    {"enabled": bool}   — enable/disable servo motion
    motion.set_limits     {"min_deg": float, "max_deg": float}  — update travel limits

Topics published:
    motion.position        {"angle": float}    — published by the servo loop (~20 Hz)
    motion.moved           {"from": float, "to": float, "direction": str}
    motion.enabled_changed {"enabled": bool}   — when enabled state changes
    motion.limits_changed  {"min_deg": float, "max_deg": float}  — when limits change

Architecture note:
    _on_pan_to is intentionally non-blocking: it stores the target angle and
    returns immediately (never sleeps, never holds the bus lock during I²C).
    A dedicated _servo_loop thread steps the servo at 50 Hz toward the target
    and publishes motion.position at ~20 Hz.  This keeps the bus lock free for
    all other publishers (encoder thread, tracking loop, etc.).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.core.bus import MessageBus
from src.core.quiet_hours import QuietHours
from src.core.service import Service

log = logging.getLogger(__name__)

_LOGICAL_HARD_MIN = 1.0
_LOGICAL_HARD_MAX = 360.0

_SERVO_STEP_INTERVAL = 0.02   # 50 Hz servo stepping rate
_POS_PUBLISH_EVERY_N = 5      # publish motion.position every 5 steps (~10 Hz)
_POSITION_DEAD_ZONE  = 0.05   # degrees — skip write if already close enough


class MotionService(Service):
    name = "motion"
    tick_seconds = 10.0   # tick is vestigial; servo loop handles position publishing

    def __init__(self, bus: Optional[MessageBus] = None, controller=None,
                 quiet_hours: Optional[QuietHours] = None,
                 servo_enabled: bool = True,
                 soft_min_deg: float = 135.0,
                 soft_max_deg: float = 215.0,
                 pulse_min_us: int = 500,
                 pulse_max_us: int = 2500) -> None:
        super().__init__(bus=bus)
        self._controller = controller
        self._quiet_hours = quiet_hours
        self._servo_enabled = servo_enabled
        self._soft_min = float(soft_min_deg)
        self._soft_max = float(soft_max_deg)
        self._pulse_min_us = int(pulse_min_us)
        self._pulse_max_us = int(pulse_max_us)
        self._unsubs = []

        # Servo loop state (protected by _target_lock)
        self._target_angle: Optional[float] = None
        self._target_speed: float = _LOGICAL_HARD_MAX   # deg/s
        self._target_lock = threading.Lock()
        self._servo_stop = threading.Event()
        self._servo_thread: Optional[threading.Thread] = None

    @property
    def servo_enabled(self) -> bool:
        return self._servo_enabled

    @property
    def soft_min_deg(self) -> float:
        return self._soft_min

    @property
    def soft_max_deg(self) -> float:
        return self._soft_max

    def on_start(self) -> None:
        if self._controller is None:
            from src.motion.servo_controller import ServoController, ServoConfig
            self._controller = ServoController(
                ServoConfig(
                    soft_min_deg=self._soft_min,
                    soft_max_deg=self._soft_max,
                    pulse_min_us=self._pulse_min_us,
                    pulse_max_us=self._pulse_max_us,
                )
            )
        else:
            self._apply_limits_to_controller()

        self._unsubs.append(self.bus.subscribe("motion.pan_to",      self._on_pan_to))
        self._unsubs.append(self.bus.subscribe("motion.relax",       self._on_relax))
        self._unsubs.append(self.bus.subscribe("motion.stop",        self._on_stop_cmd))
        self._unsubs.append(self.bus.subscribe("motion.set_enabled", self._on_set_enabled))
        self._unsubs.append(self.bus.subscribe("motion.set_limits",  self._on_set_limits))

        self._servo_stop.clear()
        self._servo_thread = threading.Thread(
            target=self._servo_loop, daemon=True, name="motion-servo"
        )
        self._servo_thread.start()

        log.info("MotionService started; hardware_ready=%s  servo_enabled=%s  limits=[%.0f-%.0f deg]",
                 getattr(self._controller, "hardware_ready", False),
                 self._servo_enabled, self._soft_min, self._soft_max)

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._controller, "hardware_ready", False))

    def run_tick(self) -> None:
        """Vestigial tick — servo loop handles position publishing."""

    # ── Non-blocking servo loop ────────────────────────────────────────

    def _servo_loop(self) -> None:
        """50 Hz loop: steps the servo toward _target_angle; publishes motion.position."""
        step_counter = 0
        while not self._servo_stop.is_set():
            t0 = time.monotonic()

            if self._controller is not None and self._servo_enabled:
                with self._target_lock:
                    target = self._target_angle
                    speed  = self._target_speed

                if target is not None:
                    current = float(self._controller.position)
                    delta = target - current
                    if abs(delta) > _POSITION_DEAD_ZONE:
                        step = speed * _SERVO_STEP_INTERVAL
                        if delta > 0:
                            new_pos = min(current + step, target)
                        else:
                            new_pos = max(current - step, target)
                        try:
                            self._controller._write(new_pos)
                        except Exception:
                            log.exception("servo _write failed")

                # Publish position ~10 Hz (every _POS_PUBLISH_EVERY_N steps)
                step_counter += 1
                if step_counter >= _POS_PUBLISH_EVERY_N:
                    step_counter = 0
                    try:
                        pos = float(self._controller.position)
                        self.bus.publish("motion.position", {"angle": pos})
                    except Exception:
                        log.exception("motion.position publish failed")

            elapsed = time.monotonic() - t0
            time.sleep(max(0.001, _SERVO_STEP_INTERVAL - elapsed))

    def on_stop(self) -> None:
        self._servo_stop.set()
        if self._servo_thread is not None:
            self._servo_thread.join(timeout=2.0)
            self._servo_thread = None

        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._controller is not None:
            try:
                current = float(self._controller.position)
                if abs(current - 180.0) > 2.0:
                    log.info("MotionService: centering head before stop (%.1f° → 180°)", current)
                    self._controller.move_to(180.0)
            except Exception:
                log.warning("MotionService: center-on-stop failed — continuing shutdown")
            try:
                self._controller.stop()
                self._controller.relax()
            except Exception:
                log.exception("controller stop/relax failed")
        log.info("MotionService stopped")

    # ---- Bus handlers -------------------------------------------------

    def _on_set_enabled(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict) or "enabled" not in payload:
            return
        new_state = bool(payload["enabled"])
        if new_state == self._servo_enabled:
            return
        self._servo_enabled = new_state
        log.info("MotionService: servo %s", "enabled" if new_state else "disabled")
        if not new_state and self._controller is not None:
            try:
                self._controller.stop()
            except Exception:
                pass
        self.bus.publish("motion.enabled_changed", {"enabled": new_state})

    def _on_set_limits(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict):
            return
        changed = False
        if "min_deg" in payload:
            new_min = max(_LOGICAL_HARD_MIN, min(float(payload["min_deg"]), _LOGICAL_HARD_MAX))
            if new_min != self._soft_min:
                self._soft_min = new_min
                changed = True
        if "max_deg" in payload:
            new_max = max(_LOGICAL_HARD_MIN, min(float(payload["max_deg"]), _LOGICAL_HARD_MAX))
            if new_max != self._soft_max:
                self._soft_max = new_max
                changed = True
        if not changed:
            return
        if self._soft_min > self._soft_max:
            self._soft_min, self._soft_max = self._soft_max, self._soft_min
        self._apply_limits_to_controller()
        log.info("MotionService: limits updated [%.0f-%.0f deg]", self._soft_min, self._soft_max)
        self.bus.publish("motion.limits_changed", {
            "min_deg": self._soft_min,
            "max_deg": self._soft_max,
        })

    def _apply_limits_to_controller(self) -> None:
        if self._controller is not None and hasattr(self._controller, "_cfg"):
            self._controller._cfg.soft_min_deg = self._soft_min
            self._controller._cfg.soft_max_deg = self._soft_max

    def _on_pan_to(self, _topic: str, payload) -> None:
        """Non-blocking: validate and store target; servo loop does the stepping."""
        if not self._servo_enabled:
            log.debug("MotionService: pan_to suppressed -- servo disabled")
            return
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("MotionService: pan_to suppressed -- quiet hours active")
            return
        if not isinstance(payload, dict) or "angle" not in payload:
            log.warning("motion.pan_to ignored: bad payload %r", payload)
            return

        angle = float(payload["angle"])
        # Clamp to current soft limits before handing to the servo loop.
        angle = max(self._soft_min, min(self._soft_max, angle))

        speed = getattr(getattr(self._controller, "_cfg", None), "speed_deg_per_sec", 90.0) if self._controller else 90.0
        move_time_ms = payload.get("move_time_ms")
        if move_time_ms is not None:
            try:
                mt = float(move_time_ms)
                if mt <= 0:
                    raise ValueError("move_time_ms must be > 0")
                current = float(self._controller.position) if self._controller else angle
                delta = abs(angle - current)
                speed = max(delta * 1000.0 / mt, 0.001)
            except Exception:
                log.warning("motion.pan_to: bad move_time_ms in payload %r", payload)
                return

        before = float(self._controller.position) if self._controller else angle
        try:
            direction = self._controller.plan_direction(before, angle) if self._controller else "?"
        except Exception:
            direction = "?"

        with self._target_lock:
            self._target_angle = angle
            self._target_speed = speed

        self.bus.publish("motion.moved", {"from": before, "to": angle, "direction": direction})

    def _on_relax(self, _topic: str, _payload) -> None:
        try:
            self._controller.relax()
        except Exception:
            log.exception("relax failed")

    def _on_stop_cmd(self, _topic: str, _payload) -> None:
        with self._target_lock:
            self._target_angle = None
        try:
            self._controller.stop()
        except Exception:
            log.exception("stop failed")
