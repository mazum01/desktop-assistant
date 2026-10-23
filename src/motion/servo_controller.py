"""
Servo controller for a SparkFun Pi Servo pHAT
(PCA9685 I²C PWM driver).

Uses Adafruit ServoKit with busio.I2C(board.SCL, board.SDA) — the
same initialisation pattern confirmed working on this hardware.

Gear-ratio calibration
-----------------------
The DS3218 has a physical range of 0°–270°.  It is geared so that
270° of servo travel produces 360° of head/camera rotation.

    gear_ratio = 360 / 270 ≈ 1.333

Pulse-width mapping (DS3218 datasheet):
    500 µs  →  0°   physical  →  0°   head
    2500 µs → 270°  physical  → 360°  head

Setting ``pulse_min_us=500`` and ``pulse_max_us=2500`` ensures the
ServoKit 0–180 angle range spans the full 0°–270° physical travel,
which in turn produces the full 0°–360° logical head rotation.

Logical pan range: 1°–360° mapped linearly onto kit angles 0°–180°.
Dead-zone wrap rule enforced: movement from higher→lower logical angle
always traverses backward; never crosses the 360°/1° boundary.

Direct-drive 180-degree servos use ``range_deg=180``: commanded angles map
directly to kit angles, with independent channels and soft limits. The legacy
360-degree gear mapping remains available for existing DS3218 installations.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# DS3218 pulse-width calibration (microseconds).
# 500–2500 µs covers the full 0°–270° physical travel of the DS3218.
# Through the 360/270 gear ratio this maps logical 1°–360° → 0°–360° head.
_PULSE_MIN_US  = 500
_PULSE_MAX_US  = 2500

# Kit angle range (ServoKit uses 0–180)
_KIT_MIN_DEG   = 0.0
_KIT_MAX_DEG   = 180.0

# Logical pan range
_LOGICAL_MIN   = 1.0
_LOGICAL_MAX   = 360.0

# Movement defaults
_DEFAULT_SPEED_DEG_PER_SEC = 90.0
_STEP_INTERVAL_SEC         = 0.02   # 50 Hz update rate


@dataclass
class ServoConfig:
    """Runtime-overridable servo parameters."""
    channel: int = 0
    i2c_address: int = 0x40             # Servo pHAT I²C address
    pulse_min_us: int = _PULSE_MIN_US
    pulse_max_us: int = _PULSE_MAX_US
    # Gear ratio: head rotation per servo rotation (DS3218 270° → 360° head)
    gear_ratio: float = 360.0 / 270.0
    speed_deg_per_sec: float = _DEFAULT_SPEED_DEG_PER_SEC
    soft_min_deg: float = _LOGICAL_MIN
    soft_max_deg: float = _LOGICAL_MAX
    range_deg: float = _LOGICAL_MAX
    # Mirror the written shaft angle for a reversed mount; logical angles,
    # limits and reported positions keep the caller's convention.
    reverse: bool = False

    def __post_init__(self) -> None:
        if type(self.channel) is not int or not 0 <= self.channel <= 15:
            raise ValueError("Servo channel must be an integer from 0 to 15")
        if not math.isfinite(self.range_deg) or not 0 < self.range_deg <= 360:
            raise ValueError("Servo range must be in (0, 360]")
        if not 0 <= self.soft_min_deg < self.soft_max_deg <= self.range_deg:
            raise ValueError("Servo limits must fit the configured range")
        if not 0 < self.pulse_min_us < self.pulse_max_us <= 2500:
            raise ValueError("Servo pulse range must be within 1-2500 microseconds")


class ServoController:
    """
    Controls the DS3218 pan servo via the SparkFun Pi Servo pHAT.
    Falls back to simulation mode when hardware is unavailable.
    """

    def __init__(self, config: Optional[ServoConfig] = None, kit=None,
                 initialize_hardware: bool = True) -> None:
        self._cfg = config or ServoConfig()
        self._current_logical: float = self._clamp_logical(self._cfg.range_deg / 2)
        self._position_initialized = False
        self._kit = kit
        if kit is not None:
            kit.servo[self._cfg.channel].set_pulse_width_range(
                self._cfg.pulse_min_us, self._cfg.pulse_max_us
            )
            return
        if not initialize_hardware:
            log.warning("Servo channel %d in simulation mode", self._cfg.channel)
            return

        try:
            import board
            import busio
            from adafruit_servokit import ServoKit

            i2c = busio.I2C(board.SCL, board.SDA)
            kit = ServoKit(
                channels=16,
                i2c=i2c,
                address=self._cfg.i2c_address,
                frequency=50,
            )
            kit.servo[self._cfg.channel].set_pulse_width_range(
                self._cfg.pulse_min_us, self._cfg.pulse_max_us
            )
            self._kit = kit
            log.info(
                "ServoController ready — PCA9685 addr=0x%02X channel=%d "
                "pulse=%d–%dµs",
                self._cfg.i2c_address, self._cfg.channel,
                self._cfg.pulse_min_us, self._cfg.pulse_max_us,
            )
        except Exception as exc:
            log.warning(
                "Servo hardware init failed (%s: %s) — SIMULATION MODE",
                type(exc).__name__, exc,
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def move_to(
        self,
        target_logical: float,
        speed_deg_per_sec: Optional[float] = None,
    ) -> None:
        """Move to target_logical degrees (1–360), respecting the wrap rule."""
        target_logical = self._clamp_logical(target_logical)
        speed = speed_deg_per_sec or self._cfg.speed_deg_per_sec
        direction = self.plan_direction(self._current_logical, target_logical)

        log.debug("move_to %.1f° from %.1f° direction=%s",
                  target_logical, self._current_logical, direction)

        step_size = speed * _STEP_INTERVAL_SEC
        pos = self._current_logical

        while True:
            if direction == "forward":
                pos = min(pos + step_size, target_logical)
                self._write(pos)
                if pos >= target_logical:
                    break
            else:
                pos = max(pos - step_size, target_logical)
                self._write(pos)
                if pos <= target_logical:
                    break
            time.sleep(_STEP_INTERVAL_SEC)

        self._current_logical = target_logical

    def set_immediate(self, logical_deg: float) -> None:
        """Jump directly to position without stepping."""
        logical_deg = self._clamp_logical(logical_deg)
        self._write(logical_deg)
        self._current_logical = logical_deg

    def set_channel(self, channel: int) -> None:
        if type(channel) is not int or not 0 <= channel <= 15:
            raise ValueError("Servo channel must be an integer from 0 to 15")
        if self._kit is not None:
            self._kit.servo[channel].set_pulse_width_range(
                self._cfg.pulse_min_us, self._cfg.pulse_max_us
            )
        self._cfg.channel = channel
        self._position_initialized = False

    def relax(self) -> None:
        """Release servo hold (de-energise)."""
        if self._kit is not None:
            self._kit.servo[self._cfg.channel].angle = None
        self._position_initialized = False

    @property
    def position_initialized(self) -> bool:
        """Whether a target has been written since initialization or release."""
        return self._position_initialized

    def stop(self) -> None:
        log.info("Servo stop at %.1f°", self._current_logical)

    def set_limits(self, min_deg: float, max_deg: float) -> None:
        """Update soft travel limits at runtime. Values are clamped to 1–360."""
        if not (math.isfinite(min_deg) and math.isfinite(max_deg)
                and 0 <= min_deg < max_deg <= self._cfg.range_deg):
            raise ValueError("Invalid servo travel limits")
        self._cfg.soft_min_deg = min_deg
        self._cfg.soft_max_deg = max_deg
        log.info("Servo travel limits updated: %.1f°–%.1f°", min_deg, max_deg)
        # Clamp current position into new limits
        clamped = self._clamp_logical(self._current_logical)
        if clamped != self._current_logical:
            self._write(clamped)
            self._current_logical = clamped

    @property
    def position(self) -> float:
        return self._current_logical

    @property
    def hardware_ready(self) -> bool:
        return self._kit is not None

    # ------------------------------------------------------------------
    # Angle math (public — no hardware required, used by tests)
    # ------------------------------------------------------------------

    @staticmethod
    def plan_direction(from_deg: float, to_deg: float) -> str:
        """Return 'forward' or 'backward'. Never cross the 360°/1° dead zone."""
        if to_deg >= from_deg:
            return "forward"
        return "backward"

    @staticmethod
    def logical_to_kit_angle(logical_deg: float) -> float:
        """
        Convert logical angle (1–360) to ServoKit angle (0–180).

        Formula: kit_angle = (logical - 1) * (180 / 359)
        """
        return (logical_deg - _LOGICAL_MIN) * (_KIT_MAX_DEG / (_LOGICAL_MAX - _LOGICAL_MIN))

    # kept for backward-compat with existing tests
    @staticmethod
    def logical_to_mechanical(logical_deg: float) -> float:
        return ServoController.logical_to_kit_angle(logical_deg)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write(self, logical_deg: float) -> None:
        logical_deg = self._clamp_logical(logical_deg)
        kit_angle = (self.logical_to_kit_angle(logical_deg)
                     if self._cfg.range_deg == 360
                     else logical_deg * 180.0 / self._cfg.range_deg)
        if self._cfg.reverse:
            kit_angle = 180.0 - kit_angle
        if self._kit is not None:
            self._kit.servo[self._cfg.channel].angle = kit_angle
        else:
            log.debug("[sim] logical=%.2f° kit_angle=%.2f°", logical_deg, kit_angle)
        self._current_logical = logical_deg
        self._position_initialized = True

    def _clamp_logical(self, deg: float) -> float:
        if not math.isfinite(deg):
            raise ValueError("Servo angle must be finite")
        return max(self._cfg.soft_min_deg, min(self._cfg.soft_max_deg, deg))
