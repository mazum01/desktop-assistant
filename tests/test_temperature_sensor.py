from unittest.mock import Mock

import pytest

from src.thermal.temperature_sensor import (
    TMP102,
    TMP102Error,
    TemperatureSensorProbeError,
    probe_temperature_sensor,
)
from src.thermal.thermal_manager import ThermalManager, ThermalThresholds


def test_probe_identifies_tmp117_by_device_id(monkeypatch):
    bus = Mock()
    bus.read_i2c_block_data.return_value = [0x01, 0x17]
    monkeypatch.setattr("src.thermal.temperature_sensor.smbus2.SMBus", Mock(return_value=bus))

    sensor = probe_temperature_sensor(bus=1, address=0x48)

    assert sensor.sensor_type == "TMP117"
    bus.read_i2c_block_data.assert_called_once_with(0x48, 0x0F, 2)
    sensor.close()


def test_probe_falls_back_to_validated_tmp102(monkeypatch):
    tmp117_bus = Mock()
    tmp117_bus.read_i2c_block_data.return_value = [0x00, 0x00]
    tmp102_bus = Mock()

    def read_tmp102(_address, register, _length):
        if register == 0x01:
            return [0x60, 0xA0]
        if register == 0x00:
            return [0x19, 0x00]
        raise AssertionError(f"Unexpected TMP102 register {register:#x}")

    tmp102_bus.read_i2c_block_data.side_effect = read_tmp102
    monkeypatch.setattr(
        "src.thermal.temperature_sensor.smbus2.SMBus",
        Mock(side_effect=[tmp117_bus, tmp102_bus]),
    )

    sensor = probe_temperature_sensor()

    assert sensor.sensor_type == "TMP102"
    assert sensor.read_temperature_c() == pytest.approx(25.0)
    assert sensor.read_temperature_f() == pytest.approx(77.0)
    tmp117_bus.close.assert_called_once_with()
    sensor.close()


def test_tmp102_extended_mode_and_negative_temperature(monkeypatch):
    bus = Mock()
    bus.read_i2c_block_data.side_effect = [
        [0x60, 0x10],
        [0xF6, 0x00],
        [0xF6, 0x00],
        [0xF6, 0x00],
    ]
    monkeypatch.setattr("src.thermal.temperature_sensor.smbus2.SMBus", Mock(return_value=bus))

    sensor = TMP102()

    assert sensor.read_temperature_c() == pytest.approx(-20.0)
    sensor.close()


def test_probe_rejects_invalid_tmp102_signature_and_closes_bus(monkeypatch):
    tmp117_bus = Mock()
    tmp117_bus.read_i2c_block_data.return_value = [0x00, 0x00]
    tmp102_bus = Mock()
    tmp102_bus.read_i2c_block_data.return_value = [0x60, 0xAF]
    monkeypatch.setattr(
        "src.thermal.temperature_sensor.smbus2.SMBus",
        Mock(side_effect=[tmp117_bus, tmp102_bus]),
    )

    with pytest.raises(TemperatureSensorProbeError, match="No supported temperature sensor"):
        probe_temperature_sensor()

    tmp102_bus.close.assert_called_once_with()


def test_probe_rejects_inconsistent_tmp102_readings(monkeypatch):
    tmp117_bus = Mock()
    tmp117_bus.read_i2c_block_data.return_value = [0x00, 0x00]
    tmp102_bus = Mock()
    tmp102_bus.read_i2c_block_data.side_effect = [
        [0x60, 0x10],
        [0x00, 0x00],
        [0x02, 0x00],
    ]
    monkeypatch.setattr(
        "src.thermal.temperature_sensor.smbus2.SMBus",
        Mock(side_effect=[tmp117_bus, tmp102_bus]),
    )

    with pytest.raises(TemperatureSensorProbeError, match="Inconsistent TMP102 probe readings"):
        probe_temperature_sensor()

    tmp102_bus.close.assert_called_once_with()


def test_tmp102_rejects_temperature_outside_device_range(monkeypatch):
    bus = Mock()
    bus.read_i2c_block_data.side_effect = [
        [0x60, 0x10],
        [0x4B, 0x80],
    ]
    monkeypatch.setattr("src.thermal.temperature_sensor.smbus2.SMBus", Mock(return_value=bus))

    with pytest.raises(TMP102Error, match="outside the TMP102 operating range"):
        TMP102()

    bus.close.assert_called_once_with()


def test_thermal_manager_uses_sensor_selected_by_probe(monkeypatch):
    fan = Mock()
    sensor = Mock(sensor_type="TMP102")
    probe = Mock(return_value=sensor)
    monkeypatch.setattr("src.thermal.thermal_manager.FanController", Mock(return_value=fan))
    monkeypatch.setattr("src.thermal.thermal_manager.probe_temperature_sensor", probe)

    manager = ThermalManager(
        thresholds=ThermalThresholds(tach_enabled=False),
        i2c_bus=2,
        i2c_address=0x49,
    )

    assert manager.sensor_type == "TMP102"
    probe.assert_called_once_with(bus=2, address=0x49)


def test_thermal_manager_sets_fan_to_full_if_sensor_probe_fails(monkeypatch):
    fan = Mock()
    monkeypatch.setattr("src.thermal.thermal_manager.FanController", Mock(return_value=fan))
    monkeypatch.setattr(
        "src.thermal.thermal_manager.probe_temperature_sensor",
        Mock(side_effect=TemperatureSensorProbeError("not detected")),
    )

    with pytest.raises(TemperatureSensorProbeError, match="not detected"):
        ThermalManager(thresholds=ThermalThresholds(tach_enabled=False))

    fan.set_duty.assert_called_once_with(100.0)
