"""
NF-A6x25 PWM fan controller.

Primary backend: kernel hardware PWM via `/sys/class/pwm/pwmchipN/pwmM`.
This delivers a clean 25 kHz signal — Noctua spec — and is silent.
Requires the `pwm-2chan` dtoverlay to route GPIO13 to PWM channel 1
of pwmchip0:

    dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4

Fallback backend: lgpio software PWM on GPIO13 at 10 kHz. Used until
the device tree overlay is in place (i.e. before the next reboot).

Fail-safe: any exception during a write commands the fan to 100% duty.
"""

from __future__ import annotations

import atexit
import logging
import os
from pathlib import Path
from typing import Optional

try:
    import lgpio
    _LGPIO_AVAILABLE = True
except ImportError:
    _LGPIO_AVAILABLE = False

log = logging.getLogger(__name__)

# Defaults — overridden by config/thermal.yaml when available.
_DEFAULT_PWM_CHIP        = 0
_DEFAULT_PWM_CHANNEL     = 1
_DEFAULT_PWM_PERIOD_NS   = 40_000        # 25 kHz
_DEFAULT_FALLBACK_GPIO   = 13
_DEFAULT_FALLBACK_FREQ   = 10_000        # lgpio sw-PWM ceiling


class FanController:
    """Controls fan duty over kernel PWM, falling back to lgpio sw-PWM."""

    def __init__(
        self,
        pwm_chip:        int = _DEFAULT_PWM_CHIP,
        pwm_channel:     int = _DEFAULT_PWM_CHANNEL,
        pwm_period_ns:   int = _DEFAULT_PWM_PERIOD_NS,
        fallback_gpio:   int = _DEFAULT_FALLBACK_GPIO,
        fallback_freq_hz: int = _DEFAULT_FALLBACK_FREQ,
        # Backwards-compat: older callers may pass `gpio_pin=13`.
        gpio_pin:        Optional[int] = None,
    ) -> None:
        if gpio_pin is not None:
            fallback_gpio = gpio_pin

        self._chip          = pwm_chip
        self._channel       = pwm_channel
        self._period_ns     = pwm_period_ns
        self._fallback_pin  = fallback_gpio
        self._fallback_freq = fallback_freq_hz

        self._duty: float                = 100.0
        self._sysfs_path: Optional[Path] = None
        self._lgpio_handle: Optional[int] = None
        self._backend = "sim"

        if not self._try_init_sysfs():
            self._try_init_lgpio()

        atexit.register(self.close)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_duty(self, duty_percent: float) -> None:
        """Set fan duty cycle (0–100 %). Clamps; fail-safes on error."""
        duty_percent = max(0.0, min(100.0, duty_percent))
        self._duty = duty_percent

        if self._backend == "sim":
            log.debug("Fan [sim] duty=%.1f%%", duty_percent)
            return

        try:
            if self._backend == "sysfs":
                self._write_sysfs_duty(duty_percent)
            elif self._backend == "lgpio":
                lgpio.tx_pwm(
                    self._lgpio_handle, self._fallback_pin,
                    self._fallback_freq, duty_percent,
                )
            log.debug("Fan duty set to %.1f%% (%s)", duty_percent, self._backend)
        except Exception:
            log.exception("Fan write failed — engaging fail-safe (100%%)")
            self._failsafe()

    @property
    def duty(self) -> float:
        return self._duty

    @property
    def backend(self) -> str:
        """Which backend is active: 'sysfs', 'lgpio', or 'sim'."""
        return self._backend

    def close(self) -> None:
        try:
            self.set_duty(100.0)   # park at full speed before exit
        except Exception:
            pass

        if self._backend == "sysfs" and self._sysfs_path is not None:
            try:
                (self._sysfs_path / "enable").write_text("0\n")
                # Leave the channel exported so we don't trip a glitch.
            except Exception:
                pass

        if self._backend == "lgpio" and self._lgpio_handle is not None:
            try:
                lgpio.gpiochip_close(self._lgpio_handle)
            except Exception:
                pass
            self._lgpio_handle = None

    # ------------------------------------------------------------------
    # Backend init
    # ------------------------------------------------------------------

    def _try_init_sysfs(self) -> bool:
        chip_path = Path(f"/sys/class/pwm/pwmchip{self._chip}")
        if not chip_path.is_dir():
            log.info("pwmchip%d not present — sysfs PWM unavailable", self._chip)
            return False

        pwm_path = chip_path / f"pwm{self._channel}"
        try:
            if not pwm_path.is_dir():
                (chip_path / "export").write_text(f"{self._channel}\n")
            (pwm_path / "period").write_text(f"{self._period_ns}\n")
            (pwm_path / "duty_cycle").write_text(f"{self._period_ns}\n")  # 100% start
            (pwm_path / "enable").write_text("1\n")
        except (OSError, PermissionError) as e:
            log.warning(
                "sysfs PWM init failed (%s) — falling back to lgpio. "
                "Did you reboot after enabling the pwm-2chan overlay?",
                e,
            )
            return False

        self._sysfs_path = pwm_path
        self._backend    = "sysfs"
        log.info(
            "FanController initialised on pwmchip%d/pwm%d at %d Hz (sysfs)",
            self._chip, self._channel, 1_000_000_000 // self._period_ns,
        )
        return True

    def _try_init_lgpio(self) -> None:
        if not _LGPIO_AVAILABLE:
            log.warning("lgpio not available — fan controller running in simulation mode")
            return
        try:
            self._lgpio_handle = lgpio.gpiochip_open(0)
            lgpio.tx_pwm(
                self._lgpio_handle, self._fallback_pin,
                self._fallback_freq, 100.0,
            )
            self._backend = "lgpio"
            log.info(
                "FanController initialised on GPIO%d at %d Hz (lgpio fallback)",
                self._fallback_pin, self._fallback_freq,
            )
        except Exception:
            log.exception("Failed to initialise lgpio fan PWM — engaging fail-safe (100%%)")
            self._failsafe()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_sysfs_duty(self, duty_percent: float) -> None:
        ns = int(self._period_ns * duty_percent / 100.0)
        # Clamp to [0, period] — sysfs rejects duty_cycle > period.
        ns = max(0, min(self._period_ns, ns))
        (self._sysfs_path / "duty_cycle").write_text(f"{ns}\n")

    def _failsafe(self) -> None:
        self._duty = 100.0
        try:
            if self._backend == "sysfs" and self._sysfs_path is not None:
                self._write_sysfs_duty(100.0)
            elif self._backend == "lgpio" and self._lgpio_handle is not None:
                lgpio.tx_pwm(
                    self._lgpio_handle, self._fallback_pin,
                    self._fallback_freq, 100.0,
                )
        except Exception:
            pass
