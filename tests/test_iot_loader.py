"""Tests for src.iot.loader — discover_types, create_device, load_persisted, save_persisted."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

# ── Helpers ───────────────────────────────────────────────────────────────────

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class _FakeBus:
    """Minimal stub for the event bus (only publishes, not subscribed to here)."""
    def publish(self, *_a, **_kw) -> None:
        pass


# ── Minimal concrete IoTDevice fixture ───────────────────────────────────────

from src.iot.base import IoTDevice


class _Widget(IoTDevice):
    device_id   = "test_widget"
    device_name = "Test Widget"
    device_icon = "🧪"

    def start(self) -> None:
        self._started = True

    def stop(self) -> None:
        self._started = False

    def get_snapshot(self) -> dict[str, Any]:
        return {
            "available": True,
            "display": {"primary": {"value": "ok"}},
            "history": [],
            "history_label": "",
        }


class _WidgetBroken(IoTDevice):
    """A device whose start() always raises."""
    device_id   = "broken_widget"
    device_name = "Broken Widget"
    device_icon = "💥"

    def start(self) -> None:
        raise RuntimeError("intentional failure")

    def stop(self) -> None:
        pass

    def get_snapshot(self) -> dict[str, Any]:
        return self._snapshot_unavailable("broken")


# ── discover_types ────────────────────────────────────────────────────────────

def test_discover_types_returns_dict(monkeypatch, tmp_path):
    """discover_types() always returns a dict (empty when devices dir has no valid modules)."""
    import src.iot.devices as _devpkg
    monkeypatch.setattr(_devpkg, "__path__", [str(tmp_path)], raising=False)
    # Reimport loader after monkeypatching
    import src.iot.loader as loader
    result = loader.discover_types()
    assert isinstance(result, dict)


def test_discover_types_finds_concrete_class(monkeypatch, tmp_path):
    """discover_types() picks up a concrete IoTDevice subclass written to a temp module."""
    widget_src = """
from src.iot.base import IoTDevice
from typing import Any

class MyDevice(IoTDevice):
    device_id = "my_device_type"
    device_name = "My Device"
    device_icon = "🔌"
    def start(self): pass
    def stop(self): pass
    def get_snapshot(self) -> dict: return self._snapshot_unavailable()
"""
    (tmp_path / "my_device.py").write_text(widget_src)
    import src.iot.devices as _devpkg
    monkeypatch.setattr(_devpkg, "__path__", [str(tmp_path)], raising=False)
    # Ensure fresh discover so our temp module can be found
    import importlib, src.iot.loader as loader
    result = loader.discover_types()
    assert "my_device_type" in result


def test_discover_types_skips_abstract(monkeypatch, tmp_path):
    """discover_types() should NOT include the IoTDevice ABC itself."""
    # Empty devices dir — IoTDevice should never appear
    import src.iot.devices as _devpkg
    monkeypatch.setattr(_devpkg, "__path__", [str(tmp_path)], raising=False)
    import src.iot.loader as loader
    result = loader.discover_types()
    from src.iot.base import IoTDevice as _base
    assert _base not in result.values()


# ── create_device ─────────────────────────────────────────────────────────────

def test_create_device_raises_on_unknown_type(monkeypatch, tmp_path):
    """create_device() raises ValueError for an unrecognised type_id."""
    import src.iot.devices as _devpkg
    monkeypatch.setattr(_devpkg, "__path__", [str(tmp_path)], raising=False)
    import src.iot.loader as loader
    with pytest.raises(ValueError, match="Unknown IoT device type"):
        loader.create_device("nonexistent_type_xyz")


def test_create_device_returns_instance(monkeypatch, tmp_path):
    """create_device() returns a correctly configured instance when type_id is valid."""
    widget_src = """
from src.iot.base import IoTDevice
class WidgetTwo(IoTDevice):
    device_id = "widget_two"
    device_name = "Widget Two"
    device_icon = "🔧"
    def start(self): pass
    def stop(self): pass
    def get_snapshot(self): return self._snapshot_unavailable()
"""
    (tmp_path / "widget_two.py").write_text(widget_src)
    import src.iot.devices as _devpkg
    monkeypatch.setattr(_devpkg, "__path__", [str(tmp_path)], raising=False)
    import src.iot.loader as loader
    dev = loader.create_device("widget_two", cfg={"host": "10.0.0.1"})
    assert dev.device_id == "widget_two"
    assert dev._cfg == {"host": "10.0.0.1"}


# ── load_persisted ────────────────────────────────────────────────────────────

from src.iot.registry import IoTRegistry


def _make_registry() -> IoTRegistry:
    return IoTRegistry()


def test_load_persisted_missing_file(tmp_path):
    """load_persisted() returns 0 and does not crash when the file doesn't exist."""
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=tmp_path / "nonexistent.json")
    assert n == 0
    assert len(reg.all()) == 0


def test_load_persisted_empty_file(tmp_path):
    """load_persisted() handles an empty JSON array gracefully."""
    persist = tmp_path / "devices.json"
    persist.write_text("[]")
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=persist)
    assert n == 0


def test_load_persisted_skips_unknown_types(tmp_path):
    """load_persisted() skips entries whose type_id has no matching class."""
    persist = tmp_path / "devices.json"
    persist.write_text(json.dumps([{"type_id": "ghost_device", "config": {}}]))
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=persist)
    assert n == 0
    assert len(reg.all()) == 0


def test_load_persisted_skips_entries_without_type_id(tmp_path):
    """load_persisted() warns and skips entries with a missing type_id field."""
    persist = tmp_path / "devices.json"
    persist.write_text(json.dumps([{"config": {"x": 1}}]))
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=persist)
    assert n == 0


def test_load_persisted_bad_json(tmp_path):
    """load_persisted() returns 0 and does not crash on malformed JSON."""
    persist = tmp_path / "devices.json"
    persist.write_text("{not valid json")
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=persist)
    assert n == 0


def test_load_persisted_not_array(tmp_path):
    """load_persisted() returns 0 when the JSON root is not an array."""
    persist = tmp_path / "devices.json"
    persist.write_text('{"device": "bad"}')
    import src.iot.loader as loader
    reg = _make_registry()
    n = loader.load_persisted(reg, path=persist)
    assert n == 0


# ── save_persisted ────────────────────────────────────────────────────────────

def test_save_persisted_empty_registry(tmp_path, monkeypatch):
    """save_persisted() writes [] when registry is empty."""
    # Patch discover_types so no types are known
    import src.iot.loader as loader
    monkeypatch.setattr(loader, "discover_types", lambda: {})
    reg = _make_registry()
    out = tmp_path / "out.json"
    loader.save_persisted(reg, path=out)
    assert json.loads(out.read_text()) == []


def test_save_persisted_skips_hardwired_devices(tmp_path, monkeypatch):
    """save_persisted() excludes devices whose type_id is not in discover_types()."""
    import src.iot.loader as loader
    # discover_types returns empty → all devices are treated as hardwired
    monkeypatch.setattr(loader, "discover_types", lambda: {})
    reg = _make_registry()
    reg.register(_Widget())
    out = tmp_path / "out.json"
    loader.save_persisted(reg, path=out)
    assert json.loads(out.read_text()) == []


def test_save_persisted_includes_plugin_devices(tmp_path, monkeypatch):
    """save_persisted() saves devices whose type_id is in discover_types()."""
    import src.iot.loader as loader
    monkeypatch.setattr(loader, "discover_types", lambda: {"test_widget": _Widget})
    reg = _make_registry()
    dev = _Widget(cfg={"key": "value"})
    reg.register(dev)
    out = tmp_path / "out.json"
    loader.save_persisted(reg, path=out)
    saved = json.loads(out.read_text())
    assert len(saved) == 1
    assert saved[0]["type_id"] == "test_widget"
    assert saved[0]["config"] == {"key": "value"}


def test_save_persisted_creates_parent_dirs(tmp_path, monkeypatch):
    """save_persisted() creates missing parent directories."""
    import src.iot.loader as loader
    monkeypatch.setattr(loader, "discover_types", lambda: {})
    reg = _make_registry()
    deep = tmp_path / "a" / "b" / "c" / "devices.json"
    loader.save_persisted(reg, path=deep)
    assert deep.exists()


def test_save_then_load_round_trip(tmp_path, monkeypatch):
    """A save-then-load round-trip correctly restores device count (when type is known)."""
    import src.iot.loader as loader

    # Make the widget discoverable
    monkeypatch.setattr(loader, "discover_types", lambda: {"test_widget": _Widget})
    monkeypatch.setattr(loader, "create_device",
                        lambda tid, cfg=None, bus=None: _Widget(cfg=cfg or {}))

    reg = _make_registry()
    reg.register(_Widget(cfg={"param": 42}))

    persist = tmp_path / "roundtrip.json"
    loader.save_persisted(reg, path=persist)

    reg2 = _make_registry()
    n = loader.load_persisted(reg2, path=persist)
    assert n == 1
    assert len(reg2.all()) == 1
    dev = reg2.get("test_widget")
    assert dev is not None
    assert dev._cfg == {"param": 42}
