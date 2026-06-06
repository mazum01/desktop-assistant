"""
Thermal manager — reads TMP117, drives fan PWM via a simple P-controller.

Thresholds (configurable via config/thermal.yaml):
  safe:     < 50 °C  — fan at minimum speed
  warm:    50–65 °C  — fan scales linearly
  critical: > 75 °C  — fan at 100%, emit warning
  failsafe: sensor error — fan at 100%
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .tmp117 import TMP117, TMP117Error
from .fan import FanController
from .fan_tach import FanTach

log = logging.getLogger(__name__)

# Resolve config path relative to the project root (two levels up from this file)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_THERMAL_CONFIG = _PROJECT_ROOT / "config" / "thermal.yaml"


@dataclass
class ThermalThresholds:
    safe_max_c: float    = 50.0
    warn_max_c: float    = 65.0
    critical_c: float    = 75.0
    fan_min_duty: float  = 30.0   # % at or below safe_max
    fan_max_duty: float  = 100.0
    poll_interval_s: float = 1.0
    tach_enabled: bool   = True   # False → skip GPIO edge-callback entirely

    @classmethod
    def from_yaml(cls, path: Path = _THERMAL_CONFIG) -> "ThermalThresholds":
        """Load thresholds from thermal.yaml, falling back to defaults on error."""
        try:
            import yaml  # type: ignore
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            t = cfg.get("thresholds", {})
            tach_enabled = bool(cfg.get("tach", {}).get("enabled", True))
            return cls(
                safe_max_c=float(t.get("safe_max_c", 50.0)),
                warn_max_c=float(t.get("warn_max_c", 65.0)),
                critical_c=float(t.get("critical_c", 75.0)),
                fan_min_duty=float(t.get("fan_min_duty", 30.0)),
                fan_max_duty=float(t.get("fan_max_duty", 100.0)),
                poll_interval_s=float(t.get("poll_interval_s", 1.0)),
                tach_enabled=tach_enabled,
            )
        except Exception as exc:
            log.warning("Could not load %s (%s) — using defaults", path, exc)
            return cls()


class ThermalManager:
    """
    Runs a background thread that polls the TMP117 and adjusts fan speed.

    Usage:
        mgr = ThermalManager()
        mgr.start()
        ...
        mgr.stop()
    """

    def __init__(
        self,
        thresholds: Optional[ThermalThresholds] = None,
        on_critical: Optional[Callable[[float], None]] = None,
        i2c_bus: int = 1,
        gpio_pin: int = 13,
        tach_gpio: int = 6,
        tach_pulses_per_rev: int = 2,
    ) -> None:
        self._thresh = thresholds or ThermalThresholds.from_yaml()
        self._on_critical = on_critical
        self._sensor = TMP117(bus=i2c_bus)
        self._fan = FanController(gpio_pin=gpio_pin)
        if self._thresh.tach_enabled:
            self._tach: Optional[FanTach] = FanTach(gpio=tach_gpio, pulses_per_rev=tach_pulses_per_rev)
        else:
            self._tach = None
            log.info("FanTach disabled via config")
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_temp_c: Optional[float] = None
        self._sensor_ok: bool = True
        self._override_duty: Optional[float] = None  # None = thermal auto mode

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the thermal control loop in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="thermal-mgr")
        self._thread.start()
        log.info("ThermalManager started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._fan.close()
        if self._tach is not None:
            self._tach.close()
        self._sensor.close()
        log.info("ThermalManager stopped")

    @property
    def temperature_c(self) -> Optional[float]:
        return self._last_temp_c

    @property
    def fan_duty(self) -> float:
        return self._fan.duty

    @property
    def fan_rpm(self) -> Optional[int]:
        return self._tach.rpm if self._tach is not None else None

    @property
    def tach_enabled(self) -> bool:
        return self._tach is not None

    @property
    def fan_backend(self) -> str:
        return self._fan.backend

    @property
    def sensor_ok(self) -> bool:
        return self._sensor_ok

    @property
    def override_active(self) -> bool:
        return self._override_duty is not None

    @property
    def override_duty(self) -> Optional[float]:
        return self._override_duty

    def set_override(self, duty_percent: float) -> None:
        """Pin fan at a fixed duty, bypassing the thermal loop."""
        duty_percent = max(0.0, min(100.0, duty_percent))
        self._override_duty = duty_percent
        self._fan.set_duty(duty_percent)
        log.info("Fan override set to %.1f%%", duty_percent)

    def clear_override(self) -> None:
        """Return fan control to the thermal loop."""
        self._override_duty = None
        log.info("Fan override cleared — returning to thermal auto mode")

    # ------------------------------------------------------------------
    # Internal control loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                temp = self._sensor.read_temperature_c()
                self._last_temp_c = temp
                self._sensor_ok = True
                if self._override_duty is None:
                    duty = self._compute_duty(temp)
                    self._fan.set_duty(duty)
                else:
                    # Override active — re-assert the pinned duty each tick so
                    # any transient failsafe can't silently steal control.
                    self._fan.set_duty(self._override_duty)
                self._check_critical(temp)
            except TMP117Error:
                if self._sensor_ok:
                    log.error("TMP117 read failed — engaging thermal fail-safe (fan 100%%)")
                self._sensor_ok = False
                self._fan.set_duty(100.0)

            self._stop_event.wait(timeout=self._thresh.poll_interval_s)

    def _compute_duty(self, temp_c: float) -> float:
        t = self._thresh
        if temp_c <= t.safe_max_c:
            return t.fan_min_duty
        if temp_c >= t.critical_c:
            return t.fan_max_duty
        # Linear scale between safe_max and critical
        ratio = (temp_c - t.safe_max_c) / (t.critical_c - t.safe_max_c)
        return t.fan_min_duty + ratio * (t.fan_max_duty - t.fan_min_duty)

    def _check_critical(self, temp_c: float) -> None:
        if temp_c >= self._thresh.critical_c:
            log.warning("CRITICAL temperature: %.2f °C", temp_c)
            if self._on_critical:
                self._on_critical(temp_c)
