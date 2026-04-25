"""
Pan servo controller for the DS3218 servo on a SparkFun Pi Servo pHAT
(PCA9685 I²C PWM driver).

Key rules enforced here:
  - Logical range: 1°–360°
  - Mechanical range: 0°–270° (DS3218)
  - The 360°/1° wrap is a DEAD ZONE — the servo can never cross it.
  - When moving from a higher angle to a lower angle, the controller
    always traverses BACKWARD (decreasing degrees) through the
    mechanical range rather than crossing the dead zone.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# DS3218 pulse-width calibration (microseconds)
_PULSE_MIN_US  = 500    # 0° mechanical
_PULSE_MAX_US  = 2500   # 270° mechanical

# Mechanical / logical constants
_MECH_MIN_DEG  = 0.0
_MECH_MAX_DEG  = 270.0
_LOGICAL_MIN   = 1.0
_LOGICAL_MAX   = 360.0

# Movement defaults
_DEFAULT_SPEED_DEG_PER_SEC  = 90.0   # degrees/second
_STEP_INTERVAL_SEC          = 0.02   # 50 Hz update rate


@dataclass
class ServoConfig:
    """Runtime-overridable servo parameters (loaded from config/servo.yaml)."""
    channel: int = 0                          # PCA9685 channel
    i2c_address: int = 0x40                   # Servo pHAT I²C address
    pulse_min_us: int = _PULSE_MIN_US
    pulse_max_us: int = _PULSE_MAX_US
    speed_deg_per_sec: float = _DEFAULT_SPEED_DEG_PER_SEC
    # Software end-stop within the logical range
    soft_min_deg: float = _LOGICAL_MIN
    soft_max_deg: float = _LOGICAL_MAX


class ServoController:
    """
    Controls the DS3218 pan servo.

    Can be used with real hardware (adafruit servokit) or in simulation
    mode when hardware is unavailable.
    """

    def __init__(self, config: Optional[ServoConfig] = None) -> None:
        self._cfg = config or ServoConfig()
        self._current_logical: float = 180.0   # start centred
        self._kit = None

        try:
            from adafruit_servokit import ServoKit  # type: ignore
            kit = ServoKit(channels=16, address=self._cfg.i2c_address)
            servo = kit.servo[self._cfg.channel]
            servo.actuation_range = int(_MECH_MAX_DEG)
            servo.set_pulse_width_range(self._cfg.pulse_min_us, self._cfg.pulse_max_us)
            self._kit = kit
            log.info(
                "ServoController ready on PCA9685 addr=0x%02X channel=%d",
                self._cfg.i2c_address,
                self._cfg.channel,
            )
        except Exception:
            log.warning("Servo hardware not available — running in simulation mode")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def move_to(
        self,
        target_logical: float,
        speed_deg_per_sec: Optional[float] = None,
    ) -> None:
        """
        Move to *target_logical* degrees (1–360), respecting the wrap rule.

        The servo will NEVER traverse through the 360°/1° dead zone.
        If target < current, movement goes backward (decreasing angle).
        If target > current, movement goes forward (increasing angle).
        """
        target_logical = self._clamp_logical(target_logical)
        speed = speed_deg_per_sec or self._cfg.speed_deg_per_sec

        direction = self._plan_direction(self._current_logical, target_logical)
        log.debug(
            "move_to %.1f° from %.1f° direction=%s",
            target_logical, self._current_logical, direction,
        )

        step_size = speed * _STEP_INTERVAL_SEC
        pos = self._current_logical

        while True:
            if direction == "forward":
                pos = min(pos + step_size, target_logical)
                self._write(pos)
                if pos >= target_logical:
                    break
            else:  # backward
                pos = max(pos - step_size, target_logical)
                self._write(pos)
                if pos <= target_logical:
                    break
            time.sleep(_STEP_INTERVAL_SEC)

        self._current_logical = target_logical

    def set_immediate(self, logical_deg: float) -> None:
        """Jump directly to position without stepping (use with caution)."""
        logical_deg = self._clamp_logical(logical_deg)
        self._write(logical_deg)
        self._current_logical = logical_deg

    @property
    def position(self) -> float:
        """Current logical position in degrees (1–360)."""
        return self._current_logical

    def stop(self) -> None:
        log.info("Servo stop requested at %.1f°", self._current_logical)

    # ------------------------------------------------------------------
    # Angle math (public for testing — no hardware required)
    # ------------------------------------------------------------------

    @staticmethod
    def plan_direction(from_deg: float, to_deg: float) -> str:
        """
        Return 'forward' or 'backward'.

        Rule: never cross the 360°/1° dead zone.
        - If to_deg > from_deg → 'forward'  (angle increases, no wrap needed)
        - If to_deg < from_deg → 'backward' (angle decreases, stays in range)
        - Equal → 'forward' (no movement)
        """
        if to_deg >= from_deg:
            return "forward"
        return "backward"

    @staticmethod
    def logical_to_mechanical(logical_deg: float) -> float:
        """
        Convert a logical angle (1–360) to mechanical angle (0–270).

        Formula: mechanical = (logical - 1) * (270 / 359)
        """
        return (logical_deg - _LOGICAL_MIN) * (_MECH_MAX_DEG / (_LOGICAL_MAX - _LOGICAL_MIN))

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _plan_direction(self, from_deg: float, to_deg: float) -> str:
        return self.plan_direction(from_deg, to_deg)

    def _write(self, logical_deg: float) -> None:
        mech = self.logical_to_mechanical(logical_deg)
        if self._kit is not None:
            self._kit.servo[self._cfg.channel].angle = mech
        else:
            log.debug("[sim] servo angle → logical=%.2f° mechanical=%.2f°", logical_deg, mech)
        self._current_logical = logical_deg

    def _clamp_logical(self, deg: float) -> float:
        return max(self._cfg.soft_min_deg, min(self._cfg.soft_max_deg, deg))
