"""Tests for src/iot/base.py and src/iot/registry.py."""

import pytest

from src.iot.base import IoTDevice
from src.iot.registry import IoTRegistry


# ── Concrete stub device for testing ─────────────────────────────────────────


class _TemperatureDevice(IoTDevice):
    device_id   = "temperature"
    device_name = "Temperature Sensor"
    device_icon = "🌡️"

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._started = False

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def get_snapshot(self) -> dict:
        return {
            "available": True,
            "error":     None,
            "display": {
                "primary": {"value": "22.5", "unit": "°C", "color": "#58a6ff"},
                "badges":  [{"text": "Normal", "color": "#3fb950"}],
                "metrics": [{"label": "Humidity", "value": "55%"}],
                "detail":  "Living room",
            },
            "history":       [10.0, 20.0, 50.0, 75.0],
            "history_label": "Temperature (°C)",
        }


class _IncompleteDevice(IoTDevice):
    """Missing device_id — should raise on register."""
    device_id   = ""
    device_name = "Incomplete"

    def start(self):  pass
    def stop(self):   pass
    def get_snapshot(self): return {}


class _UnavailableDevice(IoTDevice):
    device_id   = "unavailable_dev"
    device_name = "Unavailable"

    def start(self):  pass
    def stop(self):   pass
    def get_snapshot(self):
        return IoTDevice._snapshot_unavailable("connection refused")


# ── IoTDevice ABC ─────────────────────────────────────────────────────────────


class TestIoTDeviceABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            IoTDevice()  # missing abstract methods

    def test_concrete_device_instantiates(self):
        dev = _TemperatureDevice()
        assert dev.device_id == "temperature"
        assert dev.device_name == "Temperature Sensor"
        assert dev.device_icon == "🌡️"

    def test_start_stop(self):
        dev = _TemperatureDevice()
        assert not dev._started
        dev.start()
        assert dev._started
        dev.stop()
        assert not dev._started

    def test_get_snapshot_schema(self):
        dev = _TemperatureDevice()
        snap = dev.get_snapshot()
        assert snap["available"] is True
        assert snap["error"] is None
        assert "display" in snap
        disp = snap["display"]
        assert "primary" in disp
        assert "badges" in disp
        assert "metrics" in disp
        assert "detail" in disp
        assert isinstance(snap["history"], list)
        assert isinstance(snap["history_label"], str)

    def test_announce_available(self):
        dev = _TemperatureDevice()
        text = dev.announce()
        assert "Temperature Sensor" in text
        assert "22.5" in text
        assert "°C" in text
        assert text.endswith(".")

    def test_announce_unavailable(self):
        dev = _UnavailableDevice()
        text = dev.announce()
        assert "Unavailable" in text
        assert "connection refused" in text

    def test_snapshot_unavailable_helper(self):
        snap = IoTDevice._snapshot_unavailable("test error")
        assert snap["available"] is False
        assert snap["error"] == "test error"
        assert snap["display"]["primary"] == {}
        assert snap["history"] == []

    def test_default_cfg_is_empty_dict(self):
        dev = _TemperatureDevice()
        assert dev._cfg == {}

    def test_custom_cfg_passed(self):
        dev = _TemperatureDevice(cfg={"host": "192.168.1.1"})
        assert dev._cfg["host"] == "192.168.1.1"


# ── IoTRegistry ───────────────────────────────────────────────────────────────


class TestIoTRegistry:
    def test_empty_registry(self):
        reg = IoTRegistry()
        assert len(reg) == 0
        assert reg.all() == []

    def test_register_and_get(self):
        reg = IoTRegistry()
        dev = _TemperatureDevice()
        reg.register(dev)
        assert len(reg) == 1
        assert reg.get("temperature") is dev

    def test_get_unknown_returns_none(self):
        reg = IoTRegistry()
        assert reg.get("nonexistent") is None

    def test_register_duplicate_raises(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_TemperatureDevice())

    def test_register_missing_device_id_raises(self):
        reg = IoTRegistry()
        dev = _IncompleteDevice()
        with pytest.raises(ValueError, match="must set device_id"):
            reg.register(dev)

    def test_all_preserves_insertion_order(self):
        reg = IoTRegistry()
        dev1 = _TemperatureDevice()
        dev2 = _UnavailableDevice()
        reg.register(dev1)
        reg.register(dev2)
        all_devs = reg.all()
        assert all_devs[0].device_id == "temperature"
        assert all_devs[1].device_id == "unavailable_dev"

    def test_iter(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        reg.register(_UnavailableDevice())
        ids = [d.device_id for d in reg]
        assert "temperature" in ids
        assert "unavailable_dev" in ids

    def test_unregister(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        reg.unregister("temperature")
        assert len(reg) == 0
        assert reg.get("temperature") is None

    def test_unregister_unknown_does_not_raise(self):
        reg = IoTRegistry()
        reg.unregister("doesnotexist")  # should not raise

    def test_get_all_snapshots(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        reg.register(_UnavailableDevice())
        snaps = reg.get_all_snapshots()
        assert "temperature" in snaps
        assert "unavailable_dev" in snaps
        assert snaps["temperature"]["available"] is True
        assert snaps["unavailable_dev"]["available"] is False

    def test_get_all_snapshots_includes_device_meta(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        snap = reg.get_all_snapshots()["temperature"]
        assert snap["device_id"]   == "temperature"
        assert snap["device_name"] == "Temperature Sensor"
        assert snap["device_icon"] == "🌡️"

    def test_get_device_list(self):
        reg = IoTRegistry()
        reg.register(_TemperatureDevice())
        device_list = reg.get_device_list()
        assert len(device_list) == 1
        entry = device_list[0]
        assert entry["device_id"]   == "temperature"
        assert entry["device_name"] == "Temperature Sensor"
        assert entry["available"]   is True

    def test_get_all_snapshots_handles_exception(self):
        """A snapshot that raises should return an unavailable dict, not crash."""
        class _BrokenDevice(IoTDevice):
            device_id   = "broken"
            device_name = "Broken"
            def start(self): pass
            def stop(self):  pass
            def get_snapshot(self):
                raise RuntimeError("sensor exploded")

        reg = IoTRegistry()
        reg.register(_BrokenDevice())
        snaps = reg.get_all_snapshots()
        assert snaps["broken"]["available"] is False
