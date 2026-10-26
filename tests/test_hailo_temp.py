"""Tests for the Hailo die-temperature reader and its thermal-service wiring."""

from types import SimpleNamespace
from unittest.mock import Mock

from src.core.bus import MessageBus
from src.services.thermal_service import ThermalService
from src.thermal.hailo_temp import HailoMonitorConfig, HailoTemperatureReader


def _device(ts0=41.0, ts1=43.5):
    device = Mock()
    device.control.get_chip_temperature.return_value = SimpleNamespace(
        ts0_temperature=ts0, ts1_temperature=ts1, sample_count=1,
    )
    return device


def test_reader_reports_hottest_sensor_and_releases_device():
    device = _device()
    reading = HailoTemperatureReader(device_factory=lambda: device).read()

    assert reading.celsius == 43.5
    assert (reading.ts0_celsius, reading.ts1_celsius) == (41.0, 43.5)
    device.release.assert_called_once_with()


def test_reader_returns_none_and_releases_on_control_failure():
    device = _device()
    device.control.get_chip_temperature.side_effect = RuntimeError("HAILO_TIMEOUT")
    reader = HailoTemperatureReader(device_factory=lambda: device)

    assert reader.read() is None
    device.release.assert_called_once_with()


def test_reader_returns_none_when_device_cannot_open():
    reader = HailoTemperatureReader(device_factory=Mock(side_effect=RuntimeError("no device")))
    assert reader.read() is None


def test_config_loads_hailo_section(tmp_path):
    path = tmp_path / "thermal.yaml"
    path.write_text("hailo:\n  enabled: false\n  poll_interval_s: 0.2\n")
    cfg = HailoMonitorConfig.from_yaml(path)
    assert cfg.enabled is False
    assert cfg.poll_interval_s == 1.0  # clamped so the control channel is not hammered


def test_config_defaults_when_section_missing(tmp_path):
    path = tmp_path / "thermal.yaml"
    path.write_text("fan: {}\n")
    assert HailoMonitorConfig.from_yaml(path) == HailoMonitorConfig()


def test_thermal_service_publishes_hailo_independently_of_case_sensor():
    bus = MessageBus()
    manager = Mock(temperature_c=None, sensor_ok=False, fan_duty=100.0)
    reader = Mock()
    reader.read.side_effect = [
        SimpleNamespace(celsius=44.0, ts0_celsius=43.0, ts1_celsius=44.0),
        None,
    ]
    svc = ThermalService(bus=bus, manager=manager, hailo_reader=reader, hailo_poll_interval_s=0.0)
    svc._manager = manager
    seen = []
    bus.subscribe("thermal.hailo", lambda _t, p: seen.append(p))

    svc.run_tick()
    svc.run_tick()

    assert seen[0] == {"celsius": 44.0, "ts0_celsius": 43.0, "ts1_celsius": 44.0, "ok": True}
    assert seen[1]["ok"] is False and seen[1]["celsius"] is None


def test_thermal_service_throttles_hailo_polls():
    bus = MessageBus()
    manager = Mock(temperature_c=None, sensor_ok=False, fan_duty=100.0)
    reader = Mock()
    reader.read.return_value = None
    svc = ThermalService(bus=bus, manager=manager, hailo_reader=reader, hailo_poll_interval_s=60.0)
    svc._manager = manager

    svc.run_tick()
    svc.run_tick()

    assert reader.read.call_count == 1
