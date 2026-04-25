"""
Pan servo controller for the DS3218 servo on a SparkFun Pi Servo pHAT
(PCA9685 I²C PWM driver).

Uses Adafruit ServoKit with busio.I2C(board.SCL, board.SDA) — the
same initialisation pattern confirmed working on this hardware.

Pulse range: 750–2250 µs (calibrated for DS3218 on this pHAT).
Servo kit angle range: 0–180° (maps to the DS3218 travel range).

Logical pan range: 1°–360° mapped onto 0°–180° kit angles.
Dead-zone wrap rule enforced: movement from higher→lower logical angle
always traverses backward; never crosses the 360°/1° boundary.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger(__name__)

# DS3218 pulse-width calibration (microseconds) — confirmed working values
_PULSE_MIN_US  = 750
_PULSE_MAX_US  = 2250

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
    channel: int = 15                    # PCA9685 channel (pan servo on ch 15)
    i2c_address: int = 0x40             # Servo pHAT I²C address
    pulse_min_us: int = _PULSE_MIN_US
    pulse_max_us: int = _PULSE_MAX_US
    speed_deg_per_sec: float = _DEFAULT_SPEED_DEG_PER_SEC
    soft_min_deg: float = _LOGICAL_MIN
    soft_max_deg: float = _LOGICAL_MAX


class ServoController:
    """
    Controls the DS3218 pan servo via the SparkFun Pi Servo pHAT.
    Falls back to simulation mode when hardware is unavailable.
    """

    def __init__(self, config: Optional[ServoConfig] = None) -> None:
        self._cfg = config or ServoConfig()
        self._current_logical: float = 180.0   # start centred
        self._kit = None

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
            for i in range(16):
                kit.servo[i].set_pulse_width_range(
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

    def relax(self) -> None:
        """Release servo hold (de-energise)."""
        if self._kit is not None:
            self._kit.servo[self._cfg.channel].angle = None

    def stop(self) -> None:
        log.info("Servo stop at %.1f°", self._current_logical)

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
        kit_angle = self.logical_to_kit_angle(logical_deg)
        if self._kit is not None:
            self._kit.servo[self._cfg.channel].angle = kit_angle
        else:
            log.debug("[sim] logical=%.2f° kit_angle=%.2f°", logical_deg, kit_angle)
        self._current_logical = logical_deg

    def _clamp_logical(self, deg: float) -> float:
        return max(self._cfg.soft_min_deg, min(self._cfg.soft_max_deg, deg))

