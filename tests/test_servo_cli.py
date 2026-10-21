"""CLI tilt commands must not report success on incompatible bodies."""

from pathlib import Path
import runpy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


CLI = runpy.run_path(str(Path(__file__).parents[1] / "scripts" / "desktop-assistant"))


@pytest.mark.parametrize("status,angle", [
    ({"tilt_available": False}, 90),
    ({"tilt_available": True, "tilt_min_deg": 50, "tilt_max_deg": 90}, 100),
    ({"tilt_available": True, "tilt_min_deg": 50, "tilt_max_deg": 90}, float("nan")),
])
def test_invalid_tilt_does_not_publish(monkeypatch, status, angle):
    handler = CLI["cmd_pan"]
    request = Mock(return_value={"ok": True, "status": status})
    monkeypatch.setitem(handler.__globals__, "_request", request)
    assert handler(SimpleNamespace(axis="tilt", to=angle, move_time_ms=None)) == 1
    request.assert_called_once_with({"cmd": "motion.get_status"})


def test_valid_tilt_uses_tilt_topic(monkeypatch):
    handler = CLI["cmd_pan"]
    request = Mock(side_effect=[
        {"ok": True, "status": {
            "tilt_available": True, "tilt_min_deg": 50, "tilt_max_deg": 90,
        }},
        {"ok": True},
    ])
    monkeypatch.setitem(handler.__globals__, "_request", request)
    assert handler(SimpleNamespace(axis="tilt", to=70, move_time_ms=None)) == 0
    request.assert_called_with({
        "cmd": "publish", "topic": "motion.tilt_to",
        "payload": {"angle": 70, "override_quiet": True},
    }, timeout_ms=2000)
