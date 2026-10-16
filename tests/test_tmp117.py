from unittest.mock import Mock

import pytest

from src.thermal.tmp117 import TMP117, TMP117Error


def test_missing_sensor_reports_i2c_bus_and_closes_bus(monkeypatch):
    bus = Mock()
    bus.read_i2c_block_data.side_effect = OSError("No such device")
    monkeypatch.setattr("src.thermal.tmp117.smbus2.SMBus", Mock(return_value=bus))

    with pytest.raises(TMP117Error, match=r"address 0x48 on bus 1"):
        TMP117(bus=1)

    bus.close.assert_called_once_with()


def test_temperature_is_read_from_device(monkeypatch):
    bus = Mock()
    bus.read_i2c_block_data.side_effect = [
        [0x01, 0x17],
        [0x0C, 0x80],
    ]
    monkeypatch.setattr("src.thermal.tmp117.smbus2.SMBus", Mock(return_value=bus))

    sensor = TMP117(bus=1, address=0x49)

    assert sensor.read_temperature_c() == pytest.approx(25.0)
    bus.read_i2c_block_data.assert_any_call(0x49, 0x0F, 2)
    bus.read_i2c_block_data.assert_any_call(0x49, 0x00, 2)
    sensor.close()
