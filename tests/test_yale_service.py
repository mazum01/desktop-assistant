"""Tests for YaleService."""

from __future__ import annotations

import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

# ── Provide a minimal mock of yalesmartalarmclient for environments where the
#    real library is not installed (e.g. CI runners without the pip package).
# ────────────────────────────────────────────────────────────────────────────

def _ensure_yale_mock():
    """Insert stub modules for yalesmartalarmclient if not installed."""
    if "yalesmartalarmclient" in sys.modules:
        return  # real library present

    import enum

    class YaleLockState(enum.Enum):
        LOCKED    = 1
        UNLOCKED  = 2
        DOOR_OPEN = 3
        UNKNOWN   = 4

    class AuthenticationError(Exception):
        pass

    # Top-level package
    pkg = types.ModuleType("yalesmartalarmclient")
    pkg.YaleLockState = YaleLockState
    pkg.AuthenticationError = AuthenticationError

    lock_mod = types.ModuleType("yalesmartalarmclient.lock")
    lock_mod.YaleLockState = YaleLockState

    exc_mod = types.ModuleType("yalesmartalarmclient.exceptions")
    exc_mod.AuthenticationError = AuthenticationError

    client_mod = types.ModuleType("yalesmartalarmclient.client")
    client_mod.YaleSmartAlarmClient = MagicMock()

    sys.modules["yalesmartalarmclient"]            = pkg
    sys.modules["yalesmartalarmclient.lock"]       = lock_mod
    sys.modules["yalesmartalarmclient.exceptions"] = exc_mod
    sys.modules["yalesmartalarmclient.client"]     = client_mod


_ensure_yale_mock()

from src.services.yale_service import YaleService  # noqa: E402  (after mock setup)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_svc(cfg=None):
    return YaleService(bus=None, cfg=cfg or {"username": "test@example.com", "password": "secret"})


def _mock_lock(state_enum):
    lock = MagicMock()
    lock.name = "Front Door"
    lock.state.return_value = state_enum
    lock.autolock.return_value = True
    return lock


# ── Instantiation ─────────────────────────────────────────────────────────────


def test_degraded_when_no_credentials():
    svc = YaleService(bus=None, cfg={})
    assert svc.degraded is True
    # Should mention credentials or missing library
    reason = svc._degraded_reason.lower()
    assert "username" in reason or "password" in reason or "not installed" in reason or "not configured" in reason


def test_not_degraded_with_credentials():
    svc = _make_svc()
    # After mock setup library is present, so degraded should be False
    assert svc.degraded is False


def test_degraded_when_library_missing():
    """Simulate yalesmartalarmclient not installed by patching the import."""
    import builtins
    real_import = builtins.__import__

    def patched_import(name, *args, **kwargs):
        if name == "yalesmartalarmclient":
            raise ImportError("No module named 'yalesmartalarmclient'")
        return real_import(name, *args, **kwargs)

    # Temporarily remove from sys.modules so the import guard re-executes
    saved = {k: v for k, v in sys.modules.items() if k.startswith("yalesmartalarmclient")}
    for k in saved:
        del sys.modules[k]
    try:
        with patch("builtins.__import__", side_effect=patched_import):
            svc = YaleService(bus=None, cfg={"username": "u", "password": "p"})
        assert svc.degraded is True
        assert "not installed" in svc._degraded_reason.lower()
    finally:
        sys.modules.update(saved)


# ── get_reading before any poll ───────────────────────────────────────────────


def test_get_reading_initially_none():
    svc = _make_svc()
    assert svc.get_reading() is None


# ── _refresh ──────────────────────────────────────────────────────────────────


def test_refresh_populates_reading():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.LOCKED)

    mock_client = MagicMock()
    mock_client.lock_api.get_locks.return_value = [mock_lock]
    mock_client.lock_api.locks = [mock_lock]

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    reading = svc.get_reading()
    assert reading is not None
    assert reading["name"] == "Front Door"
    assert reading["state"] == "locked"
    assert reading["autolock"] is True


def test_refresh_unlocked_state():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    assert svc.get_reading()["state"] == "unlocked"


def test_refresh_door_open_state():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.DOOR_OPEN)
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    assert svc.get_reading()["state"] == "door_open"


def test_refresh_marks_degraded_on_auth_error():
    from yalesmartalarmclient.exceptions import AuthenticationError

    svc = _make_svc()

    with patch.object(svc, "_get_client", side_effect=AuthenticationError("bad creds")):
        svc._refresh()

    assert svc.degraded is True
    assert "authentication" in svc._degraded_reason.lower()


def test_refresh_transient_error_does_not_mark_degraded():
    """Network hiccups should not flip the degraded flag."""
    svc = _make_svc()

    with patch.object(svc, "_get_client", side_effect=ConnectionError("timeout")):
        svc._refresh()

    assert svc.degraded is False


# ── lock() / unlock() ─────────────────────────────────────────────────────────


def test_lock_success():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_lock.close.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.lock()

    assert ok is True
    assert msg == ""
    mock_lock.close.assert_called_once()


def test_lock_api_rejection():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_lock.close.return_value = False
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.lock()

    assert ok is False
    assert "rejected" in msg.lower()


def test_lock_degraded_returns_error():
    svc = YaleService(bus=None, cfg={"username": "", "password": ""})
    ok, msg = svc.lock()
    assert ok is False
    assert msg


def test_unlock_success():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.LOCKED)
    mock_lock.open.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.unlock(pin="1234")

    assert ok is True
    mock_lock.open.assert_called_once_with(pin_code="1234")


def test_unlock_no_pin_returns_error():
    svc = _make_svc()  # no unlock_pin in cfg
    ok, msg = svc.unlock(pin="")
    assert ok is False
    assert "pin" in msg.lower()


def test_unlock_uses_config_pin():
    from yalesmartalarmclient.lock import YaleLockState

    cfg = {"username": "u", "password": "p", "unlock_pin": "9999"}
    svc = _make_svc(cfg)
    mock_lock = _mock_lock(YaleLockState.LOCKED)
    mock_lock.open.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.unlock()  # no pin kwarg

    mock_lock.open.assert_called_once_with(pin_code="9999")
    assert ok is True


# ── start / stop lifecycle ────────────────────────────────────────────────────


def test_start_stop_lifecycle():
    svc = _make_svc()
    started = threading.Event()

    def slow_loop():
        started.set()
        svc._stop_event.wait(5)

    with patch.object(svc, "_poll_loop", side_effect=slow_loop):
        svc.start()
        started.wait(timeout=2)
        assert svc._thread is not None
        assert svc._thread.is_alive()
        svc.stop()
    assert not (svc._thread and svc._thread.is_alive())


def test_start_noop_when_degraded():
    svc = YaleService(bus=None, cfg={"username": "", "password": ""})
    svc.start()  # should not raise, should not create thread
    assert svc._thread is None



# ── get_reading before any poll ───────────────────────────────────────────────


def test_get_reading_initially_none():
    svc = _make_svc()
    assert svc.get_reading() is None


# ── _refresh ──────────────────────────────────────────────────────────────────


def test_refresh_populates_reading():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.LOCKED)

    mock_client = MagicMock()
    mock_client.lock_api.get_locks.return_value = [mock_lock]
    mock_client.lock_api.locks = [mock_lock]

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    reading = svc.get_reading()
    assert reading is not None
    assert reading["name"] == "Front Door"
    assert reading["state"] == "locked"
    assert reading["autolock"] is True


def test_refresh_unlocked_state():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    assert svc.get_reading()["state"] == "unlocked"


def test_refresh_door_open_state():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.DOOR_OPEN)
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        svc._refresh()

    assert svc.get_reading()["state"] == "door_open"


def test_refresh_marks_degraded_on_auth_error():
    from yalesmartalarmclient.exceptions import AuthenticationError

    svc = _make_svc()

    with patch.object(svc, "_get_client", side_effect=AuthenticationError("bad creds")):
        svc._refresh()

    assert svc.degraded is True
    assert "authentication" in svc._degraded_reason.lower()


def test_refresh_transient_error_does_not_mark_degraded():
    """Network hiccups should not flip the degraded flag."""
    svc = _make_svc()

    with patch.object(svc, "_get_client", side_effect=ConnectionError("timeout")):
        svc._refresh()

    assert svc.degraded is False


# ── lock() / unlock() ─────────────────────────────────────────────────────────


def test_lock_success():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_lock.close.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.lock()

    assert ok is True
    assert msg == ""
    mock_lock.close.assert_called_once()


def test_lock_api_rejection():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.UNLOCKED)
    mock_lock.close.return_value = False
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.lock()

    assert ok is False
    assert "rejected" in msg.lower()


def test_lock_degraded_returns_error():
    svc = _make_svc({"username": "", "password": ""})
    ok, msg = svc.lock()
    assert ok is False
    assert msg


def test_unlock_success():
    from yalesmartalarmclient.lock import YaleLockState

    svc = _make_svc()
    mock_lock = _mock_lock(YaleLockState.LOCKED)
    mock_lock.open.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.unlock(pin="1234")

    assert ok is True
    mock_lock.open.assert_called_once_with(pin_code="1234")


def test_unlock_no_pin_returns_error():
    svc = _make_svc()  # no unlock_pin in cfg
    ok, msg = svc.unlock(pin="")
    assert ok is False
    assert "pin" in msg.lower()


def test_unlock_uses_config_pin():
    from yalesmartalarmclient.lock import YaleLockState

    cfg = {"username": "u", "password": "p", "unlock_pin": "9999"}
    svc = _make_svc(cfg)
    mock_lock = _mock_lock(YaleLockState.LOCKED)
    mock_lock.open.return_value = True
    mock_client = MagicMock()

    with patch.object(svc, "_get_client", return_value=mock_client), \
         patch.object(svc, "_get_target_lock", return_value=mock_lock):
        ok, msg = svc.unlock()  # no pin kwarg

    mock_lock.open.assert_called_once_with(pin_code="9999")
    assert ok is True


# ── start / stop lifecycle ────────────────────────────────────────────────────


def test_start_stop_lifecycle():
    svc = _make_svc()
    started = threading.Event()

    def slow_loop():
        started.set()
        svc._stop_event.wait(5)

    with patch.object(svc, "_poll_loop", side_effect=slow_loop):
        svc.start()
        started.wait(timeout=2)
        assert svc._thread is not None
        assert svc._thread.is_alive()
        svc.stop()
    assert not (svc._thread and svc._thread.is_alive())


def test_start_noop_when_degraded():
    svc = _make_svc({"username": "", "password": ""})
    svc.start()  # should not raise, should not create thread
    assert svc._thread is None
