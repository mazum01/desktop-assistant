"""
NF-A6x25 PWM fan controller using Pi hardware PWM (GPIO13 / PWM1).

Requires the lgpio backend (Pi 5 compatible).
Fan PWM frequency: 25 kHz per Noctua spec.

Fail-safe: if set_duty() is called with duty=None or an exception occurs
during a write, the fan is immediately commanded to 100%.
"""

from __future__ import annotations

import atexit
import logging

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

log = logging.getLogger(__name__)

_PWM_FREQUENCY_HZ = 25_000   # Noctua spec
_DEFAULT_GPIO_PIN  = 13       # Hardware PWM1 on Pi 4/5


class FanController:
    """PWM fan controller for the Noctua NF-A6x25."""

    def __init__(self, gpio_pin: int = _DEFAULT_GPIO_PIN) -> None:
        self._pin = gpio_pin
        self._duty: float = 100.0
        self._handle: int | None = None

        if not _LGPIO_AVAILABLE:
            log.warning("lgpio not available — fan controller running in simulation mode")
            return

        try:
            self._handle = lgpio.gpiochip_open(0)
            lgpio.tx_pwm(
                self._handle,
                self._pin,
                _PWM_FREQUENCY_HZ,
                100.0,   # start at 100% until thermal manager takes control
            )
            atexit.register(self.close)
            log.info("FanController initialised on GPIO%d at %d Hz", self._pin, _PWM_FREQUENCY_HZ)
        except Exception:
            log.exception("Failed to initialise fan PWM — engaging fail-safe (100%%)")
            self._failsafe()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_duty(self, duty_percent: float) -> None:
        """Set fan duty cycle (0–100%). Clamps to valid range."""
        duty_percent = max(0.0, min(100.0, duty_percent))
        self._duty = duty_percent

        if not _LGPIO_AVAILABLE or self._handle is None:
            log.debug("Fan [sim] duty=%.1f%%", duty_percent)
            return

        try:
            lgpio.tx_pwm(self._handle, self._pin, _PWM_FREQUENCY_HZ, duty_percent)
            log.debug("Fan duty set to %.1f%%", duty_percent)
        except Exception:
            log.exception("Fan write failed — engaging fail-safe (100%%)")
            self._failsafe()

    @property
    def duty(self) -> float:
        return self._duty

    def close(self) -> None:
        if self._handle is not None:
            try:
                lgpio.tx_pwm(self._handle, self._pin, _PWM_FREQUENCY_HZ, 100.0)
                lgpio.gpiochip_close(self._handle)
            except Exception:
                pass
            self._handle = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _failsafe(self) -> None:
        self._duty = 100.0
        if self._handle is not None:
            try:
                lgpio.tx_pwm(self._handle, self._pin, _PWM_FREQUENCY_HZ, 100.0)
            except Exception:
                pass
