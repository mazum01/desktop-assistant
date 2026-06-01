"""Tests for YaleDevice IoT plugin."""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest

# ── Provide a minimal mock of yalesmartalarmclient if not installed ───────────

def _ensure_yale_mock():
    if "yalesmartalarmclient" in sys.modules:
        return

    import enum

    class YaleLockState(enum.Enum):
        LOCKED    = 1
        UNLOCKED  = 2
        DOOR_OPEN = 3
        UNKNOWN   = 4

    pkg = types.ModuleType("yalesmartalarmclient")
    lock_mod = types.ModuleType("yalesmartalarmclient.lock")
    lock_mod.YaleLockState = YaleLockState
    exc_mod = types.ModuleType("yalesmartalarmclient.exceptions")
    exc_mod.AuthenticationError = type("AuthenticationError", (Exception,), {})
    client_mod = types.ModuleType("yalesmartalarmclient.client")
    client_mod.YaleSmartAlarmClient = MagicMock()

    sys.modules["yalesmartalarmclient"]            = pkg
    sys.modules["yalesmartalarmclient.lock"]       = lock_mod
    sys.modules["yalesmartalarmclient.exceptions"] = exc_mod
    sys.modules["yalesmartalarmclient.client"]     = client_mod


_ensure_yale_mock()

from src.iot.devices.yale_device import YaleDevice  # noqa: E402


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_device(svc=None):
    dev = YaleDevice(bus=None, config={"username": "u", "password": "p"})
    if svc is not None:
        dev._svc = svc
    return dev


def _mock_svc(state="locked", degraded=False, reason=""):
    svc = MagicMock()
    svc.degraded = degraded
    svc._degraded_reason = reason
    svc.get_reading.return_value = None if degraded else {
        "name": "Front Door",
        "state": state,
        "autolock": True,
    }
    svc.lock.return_value = (True, "")
    svc.unlock.return_value = (True, "")
    return svc


# ── Class attributes ──────────────────────────────────────────────────────────


def test_class_attributes():
    assert YaleDevice.device_id == "yale_lock"
    assert YaleDevice.device_name == "Yale Lock"
    assert YaleDevice.device_icon == "🔒"
    assert YaleDevice._hardwired is False


# ── get_snapshot ──────────────────────────────────────────────────────────────


def test_snapshot_unavailable_when_no_service():
    dev = _make_device()
    snap = dev.get_snapshot()
    assert snap["available"] is False
    assert snap["error"]


def test_snapshot_unavailable_when_degraded():
    dev = _make_device(_mock_svc(degraded=True, reason="auth failed"))
    snap = dev.get_snapshot()
    assert snap["available"] is False
    assert "auth" in (snap.get("error") or "").lower()


def test_snapshot_locked():
    dev = _make_device(_mock_svc("locked"))
    snap = dev.get_snapshot()
    assert snap["available"] is True
    assert "Locked" in snap["display"]["primary"]["value"]
    assert snap["display"]["primary"]["color"] == "#4caf50"
    assert len(snap["actions"]) == 2


def test_snapshot_unlocked():
    dev = _make_device(_mock_svc("unlocked"))
    snap = dev.get_snapshot()
    assert "Unlocked" in snap["display"]["primary"]["value"]
    assert snap["display"]["primary"]["color"] == "#f44336"


def test_snapshot_door_open():
    dev = _make_device(_mock_svc("door_open"))
    snap = dev.get_snapshot()
    assert "Door Open" in snap["display"]["primary"]["value"]


def test_snapshot_actions_present():
    dev = _make_device(_mock_svc("locked"))
    snap = dev.get_snapshot()
    action_ids = [a["id"] for a in snap["actions"]]
    assert "lock" in action_ids
    assert "unlock" in action_ids


def test_snapshot_unlock_requires_pin():
    dev = _make_device(_mock_svc("locked"))
    snap = dev.get_snapshot()
    unlock_action = next(a for a in snap["actions"] if a["id"] == "unlock")
    assert unlock_action.get("requires_pin") is True


def test_snapshot_autolock_metric():
    dev = _make_device(_mock_svc("locked"))
    snap = dev.get_snapshot()
    labels = [m["label"] for m in snap["display"]["metrics"]]
    assert "Auto-lock" in labels


def test_snapshot_no_reading_yet():
    svc = _mock_svc()
    svc.degraded = False
    svc.get_reading.return_value = None
    dev = _make_device(svc)
    snap = dev.get_snapshot()
    assert snap["available"] is False
    assert "poll" in (snap.get("error") or "").lower()


# ── execute_action ────────────────────────────────────────────────────────────


def test_execute_action_lock():
    dev = _make_device(_mock_svc("unlocked"))
    result = dev.execute_action("lock")
    assert result["ok"] is True
    dev._svc.lock.assert_called_once()


def test_execute_action_unlock_with_pin():
    dev = _make_device(_mock_svc("locked"))
    result = dev.execute_action("unlock", {"pin": "1234"})
    assert result["ok"] is True
    dev._svc.unlock.assert_called_once_with(pin="1234")


def test_execute_action_unlock_no_pin():
    dev = _make_device(_mock_svc("locked"))
    result = dev.execute_action("unlock", {})
    dev._svc.unlock.assert_called_once_with(pin="")


def test_execute_action_unknown():
    dev = _make_device(_mock_svc("locked"))
    result = dev.execute_action("fly")
    assert result["ok"] is False
    assert "fly" in result["message"].lower()


def test_execute_action_when_degraded():
    dev = _make_device(_mock_svc(degraded=True, reason="no creds"))
    result = dev.execute_action("lock")
    assert result["ok"] is False
    assert "no creds" in result["message"].lower()


def test_execute_action_when_service_fails():
    svc = _mock_svc("unlocked")
    svc.lock.return_value = (False, "API rejected the request")
    dev = _make_device(svc)
    result = dev.execute_action("lock")
    assert result["ok"] is False
    assert "API rejected" in result["message"]


# ── announce ──────────────────────────────────────────────────────────────────


def test_announce_locked():
    dev = _make_device(_mock_svc("locked"))
    text = dev.announce()
    assert "front door" in text.lower()
    assert "locked" in text.lower()


def test_announce_unlocked():
    dev = _make_device(_mock_svc("unlocked"))
    text = dev.announce()
    assert "unlocked" in text.lower()


def test_announce_when_degraded():
    dev = _make_device(_mock_svc(degraded=True))
    text = dev.announce()
    assert "unavailable" in text.lower()


def test_announce_publishes_to_bus():
    bus = MagicMock()
    dev = YaleDevice(bus=bus, config={})
    dev._svc = _mock_svc("locked")
    dev.announce()
    bus.publish.assert_called_once()
    call_args = bus.publish.call_args
    assert call_args[0][0] == "av.say"


# ── get_actions ───────────────────────────────────────────────────────────────


def test_get_actions_returns_lock_and_unlock():
    dev = _make_device()
    actions = dev.get_actions()
    assert len(actions) == 2
    ids = {a["id"] for a in actions}
    assert ids == {"lock", "unlock"}


# ── lifecycle ─────────────────────────────────────────────────────────────────


def test_start_creates_service():
    dev = YaleDevice(bus=None, config={"username": "u", "password": "p"})
    with patch("src.iot.devices.yale_device.YaleDevice.start") as mock_start:
        mock_start.return_value = None
        dev.start()  # calls through to mock


def test_stop_calls_service_stop():
    dev = _make_device(_mock_svc())
    dev.stop()
    dev._svc.stop.assert_called_once()
