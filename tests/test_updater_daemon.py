"""Constrained control boundary tests."""

from unittest.mock import MagicMock

import pytest

from src.updater.daemon import Controller


def controller():
    engine = MagicMock()
    engine.state = {"settings": {"enabled": True, "start": "02:00", "end": "04:00"}}
    return Controller(engine)


@pytest.mark.parametrize("message", [
    {"operation": "execute", "payload": {"command": "anything"}},
    {"operation": "install", "payload": {"repo": "elsewhere"}},
    {"operation": "install", "payload": {"retry": "true"}},
    {"operation": "settings", "payload": {"enabled": True, "start": "99:00", "end": "04:00"}},
    {"operation": "status", "payload": {"path": "/etc"}},
])
def test_rejects_unsupported_privileged_input(message):
    with pytest.raises(ValueError):
        controller().handle(message)


def test_settings_persist():
    instance = controller()
    result = instance.handle({"operation": "settings", "payload": {
        "enabled": False, "start": "23:00", "end": "01:00",
    }})
    assert result["settings"]["enabled"] is False
    instance.engine.save.assert_called_once()


def test_serializes_requests():
    instance = controller()
    instance.busy = True
    with pytest.raises(RuntimeError, match="already"):
        instance.handle({"operation": "check", "payload": None})


def test_blocks_new_work_during_recovery():
    instance = controller()
    instance.engine.state["transaction"] = {"old": "commit"}
    with pytest.raises(RuntimeError, match="interrupted"):
        instance.handle({"operation": "install", "payload": None})
