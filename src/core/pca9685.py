"""
PCA9685 16-channel PWM driver via smbus2 (no Adafruit/Blinka dependency).

Datasheet: https://www.nxp.com/docs/en/data-sheet/PCA9685.pdf
"""

from __future__ import annotations

import math
import time
import logging

import smbus2

log = logging.getLogger(__name__)

# Register map
_MODE1          = 0x00
_MODE2          = 0x01
_PRESCALE       = 0xFE
_LED0_ON_L      = 0x06   # base; channel n starts at 0x06 + 4*n
_ALLCALL        = 0x01
_SLEEP          = 0x10
_RESTART        = 0x80

_INTERNAL_OSC_HZ = 25_000_000   # 25 MHz internal oscillator


class PCA9685Error(Exception):
    pass


class PCA9685:
    """Minimal PCA9685 driver using smbus2."""

    def __init__(
        self,
        bus: int = 1,
        address: int = 0x40,
        frequency_hz: float = 50.0,
    ) -> None:
        self._bus = smbus2.SMBus(bus)
        self._addr = address
        self._reset()
        self.set_frequency(frequency_hz)
        log.info("PCA9685 ready at I²C addr=0x%02X, freq=%.1f Hz", address, frequency_hz)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_frequency(self, frequency_hz: float) -> None:
        """Set PWM frequency for all channels (Hz)."""
        prescale = round(_INTERNAL_OSC_HZ / (4096 * frequency_hz)) - 1
        prescale = max(3, min(255, prescale))

        old_mode = self._read(_MODE1)
        # Enter sleep to change prescaler
        self._write(_MODE1, (old_mode & 0x7F) | _SLEEP)
        self._write(_PRESCALE, prescale)
        self._write(_MODE1, old_mode)
        time.sleep(0.0005)
        self._write(_MODE1, old_mode | _RESTART)
        self._frequency_hz = frequency_hz

    def set_pwm(self, channel: int, on: int, off: int) -> None:
        """Set raw ON/OFF 12-bit counts (0–4095) for a channel."""
        base = _LED0_ON_L + 4 * channel
        self._bus.write_i2c_block_data(
            self._addr, base,
            [on & 0xFF, (on >> 8) & 0x0F, off & 0xFF, (off >> 8) & 0x0F],
        )

    def set_pulse_us(self, channel: int, pulse_us: float) -> None:
        """Set pulse width in microseconds for a channel."""
        period_us = 1_000_000.0 / self._frequency_hz
        off = round(pulse_us / period_us * 4096)
        off = max(0, min(4095, off))
        self.set_pwm(channel, 0, off)

    def close(self) -> None:
        self._bus.close()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _reset(self) -> None:
        try:
            self._write(_MODE1, _ALLCALL)
            time.sleep(0.0005)
            mode1 = self._read(_MODE1) & ~_SLEEP
            self._write(_MODE1, mode1)
            time.sleep(0.0005)
        except OSError as exc:
            raise PCA9685Error(
                f"PCA9685 not found at 0x{self._addr:02X}"
            ) from exc

    def _write(self, register: int, value: int) -> None:
        self._bus.write_byte_data(self._addr, register, value)

    def _read(self, register: int) -> int:
        return self._bus.read_byte_data(self._addr, register)
