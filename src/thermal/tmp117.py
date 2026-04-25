"""
TMP117 High-Accuracy Temperature Sensor driver.

Communicates over I²C (Qwiic / STEMMA QT).
Default I²C address: 0x48.

Datasheet: https://www.ti.com/lit/ds/symlink/tmp117.pdf
"""

from __future__ import annotations

import struct
import time

import smbus2

# Register map
_REG_TEMP_RESULT = 0x00   # 16-bit 2's complement, 1 LSB = 7.8125e-3 °C
_REG_CONFIG      = 0x01
_REG_DEVICE_ID   = 0x0F

_DEVICE_ID_VALUE = 0x0117
_LSB_DEG_C       = 7.8125e-3   # °C per LSB


class TMP117Error(Exception):
    """Raised when the TMP117 is unreachable or returns bad data."""


class TMP117:
    """Minimal driver for the SparkFun Qwiic TMP117 Breakout."""

    def __init__(self, bus: int = 1, address: int = 0x48) -> None:
        self._bus = smbus2.SMBus(bus)
        self._addr = address
        self._verify_device()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def read_temperature_c(self) -> float:
        """Return temperature in degrees Celsius."""
        raw = self._read_register(_REG_TEMP_RESULT)
        # 16-bit 2's complement
        if raw > 0x7FFF:
            raw -= 0x10000
        return raw * _LSB_DEG_C

    def read_temperature_f(self) -> float:
        """Return temperature in degrees Fahrenheit."""
        return self.read_temperature_c() * 9.0 / 5.0 + 32.0

    def close(self) -> None:
        self._bus.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _verify_device(self) -> None:
        try:
            device_id = self._read_register(_REG_DEVICE_ID)
        except OSError as exc:
            raise TMP117Error(
                f"TMP117 not found at I²C address 0x{self._addr:02X} "
                f"on bus {self._bus.fd}"
            ) from exc
        if device_id != _DEVICE_ID_VALUE:
            raise TMP117Error(
                f"Unexpected device ID 0x{device_id:04X} "
                f"(expected 0x{_DEVICE_ID_VALUE:04X})"
            )

    def _read_register(self, register: int) -> int:
        data = self._bus.read_i2c_block_data(self._addr, register, 2)
        return (data[0] << 8) | data[1]
