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
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .tmp117 import TMP117, TMP117Error
from .fan import FanController
from .fan_tach import FanTach

log = logging.getLogger(__name__)


@dataclass
class ThermalThresholds:
    safe_max_c: float    = 50.0
    warn_max_c: float    = 65.0
    critical_c: float    = 75.0
    fan_min_duty: float  = 30.0   # % at or below safe_max
    fan_max_duty: float  = 100.0
    poll_interval_s: float = 1.0


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
        self._thresh = thresholds or ThermalThresholds()
        self._on_critical = on_critical
        self._sensor = TMP117(bus=i2c_bus)
        self._fan = FanController(gpio_pin=gpio_pin)
        self._tach = FanTach(gpio=tach_gpio, pulses_per_rev=tach_pulses_per_rev)
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_temp_c: Optional[float] = None
        self._sensor_ok: bool = True

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
        return self._tach.rpm

    @property
    def fan_backend(self) -> str:
        return self._fan.backend

    @property
    def sensor_ok(self) -> bool:
        return self._sensor_ok

    # ------------------------------------------------------------------
    # Internal control loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                temp = self._sensor.read_temperature_c()
                self._last_temp_c = temp
                self._sensor_ok = True
                duty = self._compute_duty(temp)
                self._fan.set_duty(duty)
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
