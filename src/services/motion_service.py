"""
Motion service.

Wraps two `ServoController` instances sharing one PCA9685, with independent
pan/tilt targets. Motion is disabled together and channels must be distinct.

Topics subscribed:
    motion.pan_to         {"angle": float, "move_time_ms"?: float}
    motion.tilt_to        {"angle": float, "move_time_ms"?: float}
    motion.center         {"override_quiet"?: bool}
    motion.set_channels   {"pan_channel": int, "tilt_channel": int}
    motion.set_tilt_limits {"min_deg": float, "max_deg": float}
    motion.relax          None
    motion.stop           None
    motion.set_enabled    {"enabled": bool}   — enable/disable servo motion
    motion.set_limits     {"min_deg": float, "max_deg": float}  — update travel limits

Topics published:
    motion.position        {"angle": float, "tilt_angle": float | None}
    motion.settings        Current limits, channels, range and hardware state
    motion.channels_changed / motion.tilt_limits_changed  Persistent settings
    motion.error           {"error": str}
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
import math
import threading
import time
from typing import Optional

from src.core.bus import MessageBus
from src.core.quiet_hours import QuietHours
from src.core.service import Service

log = logging.getLogger(__name__)

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
                 pulse_max_us: int = 2500,
                 pan_channel: int = 0,
                 tilt_channel: int = 1,
                 range_deg: float = 360.0,
                 tilt_min_deg: float = 70.0,
                 tilt_max_deg: float = 110.0,
                 tilt_controller=None,
                 tilt_enabled: bool = False) -> None:
        super().__init__(bus=bus)
        self._controller = controller
        self._quiet_hours = quiet_hours
        self._servo_enabled = servo_enabled
        self._soft_min = float(soft_min_deg)
        self._soft_max = float(soft_max_deg)
        self._pulse_min_us = int(pulse_min_us)
        self._pulse_max_us = int(pulse_max_us)
        self._pan_channel = pan_channel
        self._tilt_channel = tilt_channel
        self._range_deg = range_deg
        self._tilt_min = tilt_min_deg
        self._tilt_max = tilt_max_deg
        self._tilt_controller = tilt_controller
        self._tilt_enabled = tilt_enabled or tilt_controller is not None
        self._tilt_target = None
        self._tilt_speed = 90.0
        if self._tilt_enabled:
            self._validate_channels(pan_channel, tilt_channel)
        elif type(pan_channel) is not int or not 0 <= pan_channel <= 15:
            raise ValueError("Pan channel must be an integer from 0 to 15")
        self._unsubs = []

        # Servo loop state (protected by _target_lock)
        self._target_angle: Optional[float] = None
        self._target_speed: float = _LOGICAL_HARD_MAX   # deg/s
        self._target_lock = threading.RLock()
        self._profiles: dict = {}
        self._calibration_until = 0.0
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

    def get_status(self) -> dict:
        return {
            "servo_enabled": self._servo_enabled,
            "soft_min_deg": self._soft_min, "soft_max_deg": self._soft_max,
            "pan_channel": self._pan_channel, "tilt_channel": self._tilt_channel,
            "tilt_min_deg": self._tilt_min, "tilt_max_deg": self._tilt_max,
            "range_deg": self._range_deg,
            "pulse_min_us": self._pulse_min_us, "pulse_max_us": self._pulse_max_us,
            "hardware_ready": self.hardware_ready,
            "tilt_available": bool(
                self._tilt_enabled and self._tilt_controller is not None
                and self._tilt_controller.hardware_ready
            ),
        }

    @staticmethod
    def _validate_channels(pan, tilt) -> None:
        if any(type(ch) is not int or not 0 <= ch <= 15 for ch in (pan, tilt)):
            raise ValueError("Channels must be integers from 0 to 15")
        if pan == tilt:
            raise ValueError("Pan and tilt channels must be different")

    def set_channels(self, pan: int, tilt: int) -> None:
        self._validate_channels(pan, tilt)
        with self._target_lock:
            if self._controller is None or self._tilt_controller is None:
                raise ValueError("Servo controllers are not initialized")
            self._target_angle = self._tilt_target = None
            self._profiles.clear()
            try:
                for controller in (self._controller, self._tilt_controller):
                    controller.relax()
                self._controller.set_channel(pan)
                self._tilt_controller.set_channel(tilt)
            except Exception:
                self._servo_enabled = False
                log.exception("Channel reassignment failed; disabling motion")
                self.bus.publish("motion.enabled_changed", {"enabled": False})
                self.bus.publish("motion.error", {"error": "Channel reassignment failed; motion disabled"})
                try:
                    self._controller.set_channel(self._pan_channel)
                    self._tilt_controller.set_channel(self._tilt_channel)
                except Exception:
                    log.exception("Failed to restore original servo channels")
                raise
            self._pan_channel, self._tilt_channel = pan, tilt
        self.bus.publish("motion.channels_changed", {
            "pan_channel": pan, "tilt_channel": tilt,
        })
        self.bus.publish("motion.settings", self.get_status())

    def on_start(self) -> None:
        if self._controller is None:
            from src.motion.servo_controller import ServoController, ServoConfig
            self._controller = ServoController(
                ServoConfig(
                    soft_min_deg=self._soft_min,
                    soft_max_deg=self._soft_max,
                    pulse_min_us=self._pulse_min_us,
                    pulse_max_us=self._pulse_max_us,
                    channel=self._pan_channel,
                    range_deg=self._range_deg,
                )
            )
        else:
            self._apply_limits_to_controller()

        if (self._tilt_enabled and self._tilt_controller is None
                and hasattr(self._controller, "_kit")):
            from src.motion.servo_controller import ServoController, ServoConfig
            self._tilt_controller = ServoController(ServoConfig(
                channel=self._tilt_channel, range_deg=180,
                soft_min_deg=self._tilt_min, soft_max_deg=self._tilt_max,
                pulse_min_us=self._pulse_min_us, pulse_max_us=self._pulse_max_us,
            ), kit=self._controller._kit, initialize_hardware=False)
        self._unsubs.append(self.bus.subscribe("motion.pan_to",      self._on_pan_to))
        self._unsubs.append(self.bus.subscribe("motion.tilt_to", self._on_tilt_to))
        self._unsubs.append(self.bus.subscribe("motion.set_channels", self._on_set_channels))
        self._unsubs.append(self.bus.subscribe("motion.set_tilt_limits", self._on_tilt_limits))
        self._unsubs.append(self.bus.subscribe("motion.center", self._on_center))
        self._unsubs.append(self.bus.subscribe("motion.relax",       self._on_relax))
        self._unsubs.append(self.bus.subscribe("motion.stop",        self._on_stop_cmd))
        self._unsubs.append(self.bus.subscribe("motion.set_enabled", self._on_set_enabled))
        self._unsubs.append(self.bus.subscribe("motion.set_limits",  self._on_set_limits))
        self._unsubs.append(self.bus.subscribe("motion.calibration_lock", self._on_calibration_lock))

        self._servo_stop.clear()
        self._servo_thread = threading.Thread(
            target=self._servo_loop, daemon=True, name="motion-servo"
        )
        self._servo_thread.start()
        self.bus.publish("motion.settings", self.get_status())

        log.info("MotionService started; hardware_ready=%s  servo_enabled=%s  limits=[%.0f-%.0f deg]",
                 getattr(self._controller, "hardware_ready", False),
                 self._servo_enabled, self._soft_min, self._soft_max)

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._controller, "hardware_ready", False))

    def run_tick(self) -> None:
        """Vestigial tick — servo loop handles position publishing."""

    # ── Non-blocking servo loop ────────────────────────────────────────

    def _step_axis(self, controller, target, speed) -> None:
        if controller is None or target is None:
            return
        current = float(controller.position)
        profile = self._profiles.get(id(controller))
        if profile is not None:
            started, duration, initial, final = profile
            fraction = min(1.0, max(0.0, (time.monotonic() - started) / duration))
            eased = fraction ** 3 * (10 - 15 * fraction + 6 * fraction ** 2)
            controller._write(initial + (final - initial) * eased)
            if fraction >= 1:
                self._profiles.pop(id(controller), None)
            return
        delta = target - current
        if abs(delta) > _POSITION_DEAD_ZONE:
            step = math.copysign(min(abs(delta), speed * _SERVO_STEP_INTERVAL), delta)
            controller._write(current + step)
        elif not getattr(controller, "position_initialized", True):
            controller._write(target)

    def _servo_loop(self) -> None:
        """50 Hz loop: steps the servo toward _target_angle; publishes motion.position."""
        step_counter = 0
        while not self._servo_stop.is_set():
            t0 = time.monotonic()

            if self._controller is not None and self._servo_enabled:
                with self._target_lock:
                    try:
                        self._step_axis(self._controller, self._target_angle, self._target_speed)
                        self._step_axis(self._tilt_controller, self._tilt_target, self._tilt_speed)
                    except Exception:
                        log.exception("servo _write failed")
                        self._target_angle = self._tilt_target = None
                        self._profiles.clear()
                        self.bus.publish("motion.error", {"error": "Servo write failed"})

                # Publish position ~10 Hz (every _POS_PUBLISH_EVERY_N steps)
                step_counter += 1
                if step_counter >= _POS_PUBLISH_EVERY_N:
                    step_counter = 0
                    try:
                        pos = float(self._controller.position)
                        self.bus.publish("motion.position", {
                            "angle": pos,
                            "tilt_angle": (float(self._tilt_controller.position)
                                           if self._tilt_controller else None),
                        })
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
                center = (self._soft_min + self._soft_max) / 2
                if abs(current - center) > 2.0:
                    self._controller.move_to(center)
            except Exception:
                log.warning("MotionService: center-on-stop failed — continuing shutdown")
            try:
                self._controller.stop()
                self._controller.relax()
            except Exception:
                log.exception("controller stop/relax failed")
        log.info("MotionService stopped")
        if self._tilt_controller is not None:
            try:
                self._tilt_controller.stop()
                self._tilt_controller.relax()
            except Exception:
                log.exception("tilt controller stop/relax failed")

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
            self._on_relax(_topic, None)
        self.bus.publish("motion.enabled_changed", {"enabled": new_state})
        self.bus.publish("motion.settings", self.get_status())

    def _on_set_limits(self, _topic: str, payload) -> None:
        if not isinstance(payload, dict):
            log.warning("motion.set_limits ignored: bad payload %r", payload)
            return
        try:
            mn = float(payload.get("min_deg", self._soft_min))
            mx = float(payload.get("max_deg", self._soft_max))
            if not 0 <= mn < mx <= self._range_deg:
                raise ValueError("Limits outside configured servo range")
        except (ValueError, TypeError):
            log.warning("motion.set_limits ignored: bad payload %r", payload)
            return
        with self._target_lock:
            self._soft_min, self._soft_max = mn, mx
            self._profiles.clear()
            self._apply_limits_to_controller()
            if self._target_angle is not None:
                self._target_angle = max(mn, min(mx, self._target_angle))
        log.info("MotionService: limits updated [%.0f-%.0f deg]", self._soft_min, self._soft_max)
        self.bus.publish("motion.limits_changed", {
            "min_deg": self._soft_min,
            "max_deg": self._soft_max,
        })
        self.bus.publish("motion.settings", self.get_status())

    def _apply_limits_to_controller(self) -> None:
        if self._controller is not None and hasattr(self._controller, "_cfg"):
            self._controller._cfg.soft_min_deg = self._soft_min
            self._controller._cfg.soft_max_deg = self._soft_max

    def _on_pan_to(self, _topic: str, payload) -> None:
        """Non-blocking: validate and store target; servo loop does the stepping."""
        if (time.monotonic() < self._calibration_until
                and not (isinstance(payload, dict) and payload.get("source") == "calibration")):
            return
        if not self._servo_enabled:
            log.debug("MotionService: pan_to suppressed -- servo disabled")
            return
        if self._quiet_hours and self._quiet_hours.is_quiet():
            # Explicit user commands (CLI, web GUI, OpenClaw) carry
            # "override_quiet": true to bypass quiet hours. Autonomous
            # tracking/random-motion commands don't, so they stay silent.
            if not (isinstance(payload, dict) and payload.get("override_quiet")):
                log.debug("MotionService: pan_to suppressed -- quiet hours active")
                return
        if not isinstance(payload, dict) or "angle" not in payload:
            log.warning("motion.pan_to ignored: bad payload %r", payload)
            return

        try:
            angle = float(payload["angle"])
            if not math.isfinite(angle):
                raise ValueError("Non-finite angle")
        except (ValueError, TypeError):
            log.warning("%s ignored: invalid angle %r", _topic, payload)
            return
        # Clamp to current soft limits before handing to the servo loop.
        angle = max(self._soft_min, min(self._soft_max, angle))

        speed = getattr(getattr(self._controller, "_cfg", None), "speed_deg_per_sec", 90.0) if self._controller else 90.0
        move_time_ms = payload.get("move_time_ms")
        if move_time_ms is not None:
            try:
                mt = float(move_time_ms)
                if not math.isfinite(mt) or mt <= 0:
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
            self._set_profile(self._controller, angle, payload)

        self.bus.publish("motion.moved", {"from": before, "to": angle, "direction": direction})

    def _on_tilt_to(self, _topic: str, payload) -> None:
        if (time.monotonic() < self._calibration_until
                and not (isinstance(payload, dict) and payload.get("source") == "calibration")):
            return
        if self._tilt_controller is None:
            log.error("motion.tilt_to rejected: tilt hardware unavailable")
            self.bus.publish("motion.error", {"error": "Tilt hardware unavailable"})
            return
        if not self._servo_enabled:
            return
        if self._quiet_hours and self._quiet_hours.is_quiet():
            if not (isinstance(payload, dict) and payload.get("override_quiet")):
                return
        try:
            if not isinstance(payload, dict):
                raise ValueError("Expected an angle payload")
            angle = float(payload["angle"])
            duration = float(payload.get("move_time_ms", 1000))
            if not math.isfinite(angle) or not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid angle or move time")
            angle = max(self._tilt_min, min(self._tilt_max, angle))
        except (KeyError, ValueError, TypeError):
            log.warning("motion.tilt_to ignored: bad payload %r", payload)
            return
        with self._target_lock:
            self._tilt_target = angle
            self._tilt_speed = max(abs(angle - self._tilt_controller.position) * 1000 / duration, 0.001)
            self._set_profile(self._tilt_controller, angle, payload)

    def _set_profile(self, controller, target, payload):
        self._profiles.pop(id(controller), None)
        if payload.get("profile") == "smoothstep":
            duration = float(payload["move_time_ms"]) / 1000
            self._profiles[id(controller)] = (
                time.monotonic(), duration, float(controller.position), target,
            )

    def _on_calibration_lock(self, _topic, payload):
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            log.error("Invalid calibration lock request: %r", payload)
            return
        with self._target_lock:
            was_locked = time.monotonic() < self._calibration_until
            self._calibration_until = time.monotonic() + 60 if payload["enabled"] else 0
            if not was_locked or not payload["enabled"]:
                self._target_angle = self._tilt_target = None
                self._profiles.clear()

    def _on_center(self, _topic: str, payload) -> None:
        override = isinstance(payload, dict) and payload.get("override_quiet", False)
        self._on_pan_to("motion.pan_to", {
            "angle": (self._soft_min + self._soft_max) / 2, "override_quiet": override,
        })
        if self._tilt_controller is not None:
            self._on_tilt_to("motion.tilt_to", {
                "angle": (self._tilt_min + self._tilt_max) / 2, "override_quiet": override,
            })

    def _on_set_channels(self, _topic: str, payload) -> None:
        try:
            self.set_channels(payload["pan_channel"], payload["tilt_channel"])
        except (ValueError, TypeError, KeyError, AttributeError):
            log.exception("motion.set_channels failed")
            self.bus.publish("motion.error", {"error": "Unable to set servo channels"})

    def _on_tilt_limits(self, _topic: str, payload) -> None:
        try:
            mn, mx = float(payload["min_deg"]), float(payload["max_deg"])
            if not (0 <= mn < mx <= 180):
                raise ValueError("Tilt limits must be within 0-180")
            with self._target_lock:
                self._tilt_min, self._tilt_max = mn, mx
                self._profiles.clear()
                if self._tilt_controller:
                    self._tilt_controller._cfg.soft_min_deg = mn
                    self._tilt_controller._cfg.soft_max_deg = mx
                if self._tilt_target is not None:
                    self._tilt_target = max(mn, min(mx, self._tilt_target))
            self.bus.publish("motion.tilt_limits_changed", {"min_deg": mn, "max_deg": mx})
            self.bus.publish("motion.settings", self.get_status())
        except (ValueError, TypeError, KeyError):
            log.exception("motion.set_tilt_limits failed")

    def _on_relax(self, _topic: str, _payload) -> None:
        try:
            with self._target_lock:
                self._target_angle = self._tilt_target = None
                self._profiles.clear()
                self._controller.relax()
                if self._tilt_controller:
                    self._tilt_controller.relax()
        except Exception:
            log.exception("relax failed")

    def _on_stop_cmd(self, _topic: str, _payload) -> None:
        with self._target_lock:
            self._target_angle = self._tilt_target = None
            self._profiles.clear()
        try:
            self._controller.stop()
            if self._tilt_controller:
                self._tilt_controller.stop()
        except Exception:
            log.exception("stop failed")
