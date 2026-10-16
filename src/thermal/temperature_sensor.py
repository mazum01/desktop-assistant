"""Probe and read supported I2C temperature sensors."""

from __future__ import annotations

import logging

import smbus2

from .tmp117 import TMP117, TMP117Error

log = logging.getLogger(__name__)

_TMP102_TEMP_REGISTER = 0x00
_TMP102_CONFIG_REGISTER = 0x01
_TMP102_CONFIG_RESERVED_MASK = 0x000F
_TMP102_EXTENDED_MODE = 0x0010
_TMP102_LSB_DEG_C = 0.0625
_TMP102_MIN_C = -55.0
_TMP102_MAX_C = 150.0
_TMP102_MAX_PROBE_DELTA_C = 2.0


class TemperatureSensorProbeError(Exception):
    """Raised when no supported temperature sensor can be identified."""


class TMP102Error(TemperatureSensorProbeError):
    """Raised when the TMP102 is unreachable or returns invalid data."""


class TMP102:
    """TMP102 temperature sensor using its 12-bit or extended 13-bit format."""

    sensor_type = "TMP102"

    def __init__(self, bus: int = 1, address: int = 0x48) -> None:
        self._bus_number = bus
        self._address = address
        try:
            self._bus = smbus2.SMBus(bus)
        except OSError as exc:
            raise TMP102Error(f"Could not open I2C bus {bus}: {exc}") from exc
        try:
            config = self._read_register(_TMP102_CONFIG_REGISTER)
            if config & _TMP102_CONFIG_RESERVED_MASK:
                raise TMP102Error(
                    f"Unexpected TMP102 configuration 0x{config:04X} "
                    f"at I2C address 0x{address:02X}"
                )
            self._extended_mode = bool(config & _TMP102_EXTENDED_MODE)
            first = self._read_temperature_c()
            second = self._read_temperature_c()
            if abs(first - second) > _TMP102_MAX_PROBE_DELTA_C:
                raise TMP102Error(
                    f"Inconsistent TMP102 probe readings "
                    f"{first:.2f} C and {second:.2f} C"
                )
        except TMP102Error:
            self._bus.close()
            raise

    def read_temperature_c(self) -> float:
        """Return the TMP102 temperature in degrees Celsius."""
        return self._read_temperature_c()

    def read_temperature_f(self) -> float:
        """Return the TMP102 temperature in degrees Fahrenheit."""
        return self.read_temperature_c() * 9.0 / 5.0 + 32.0

    def close(self) -> None:
        self._bus.close()

    def _read_temperature_c(self) -> float:
        raw = self._read_register(_TMP102_TEMP_REGISTER)
        shift = 3 if self._extended_mode else 4
        if raw & ((1 << shift) - 1):
            raise TMP102Error(
                f"Temperature register 0x{raw:04X} does not match "
                f"TMP102 {'13' if self._extended_mode else '12'}-bit format"
            )
        width = 16 - shift
        value = raw >> shift
        if value & (1 << (width - 1)):
            value -= 1 << width
        temperature_c = value * _TMP102_LSB_DEG_C
        if not _TMP102_MIN_C <= temperature_c <= _TMP102_MAX_C:
            raise TMP102Error(
                f"Temperature {temperature_c:.2f} C is outside "
                "the TMP102 operating range"
            )
        return temperature_c

    def _read_register(self, register: int) -> int:
        try:
            data = self._bus.read_i2c_block_data(self._address, register, 2)
        except OSError as exc:
            raise TMP102Error(
                f"Could not read I2C address 0x{self._address:02X} "
                f"on bus {self._bus_number}: {exc}"
            ) from exc
        if len(data) != 2:
            raise TMP102Error(
                f"Expected 2 bytes from I2C address 0x{self._address:02X}, "
                f"received {len(data)}"
            )
        return (data[0] << 8) | data[1]


def probe_temperature_sensor(bus: int = 1, address: int = 0x48) -> TMP117 | TMP102:
    """Identify a TMP117 by ID or a TMP102 by its register format."""
    try:
        sensor = TMP117(bus=bus, address=address)
    except (TMP117Error, OSError) as tmp117_error:
        tmp117_message = str(tmp117_error)
        log.debug(
            "TMP117 identification failed at I2C bus %d address 0x%02X: %s",
            bus,
            address,
            tmp117_error,
        )
    else:
        log.info(
            "Detected %s by device ID at I2C bus %d address 0x%02X",
            sensor.sensor_type,
            bus,
            address,
        )
        return sensor

    try:
        sensor = TMP102(bus=bus, address=address)
    except (TMP102Error, OSError) as tmp102_error:
        raise TemperatureSensorProbeError(
            f"No supported temperature sensor at I2C bus {bus}, address "
            f"0x{address:02X}; TMP117 probe: {tmp117_message}; "
            f"TMP102 probe: {tmp102_error}"
        ) from tmp102_error

    log.info("Detected %s at I2C bus %d address 0x%02X", sensor.sensor_type, bus, address)
    return sensor
