"""Tests for SystemStatusSkill."""
from unittest.mock import MagicMock

from src.skills.system_status_skill import SystemStatusSkill


def _bus():
    return MagicMock()


def test_matches_how_hot():
    skill = SystemStatusSkill(live_data={})
    assert skill.match("how hot are you") is not None


def test_matches_temperature():
    skill = SystemStatusSkill(live_data={})
    assert skill.match("what is the temperature") is not None


def test_matches_status_report():
    skill = SystemStatusSkill(live_data={})
    assert skill.match("status report") is not None


def test_no_match():
    skill = SystemStatusSkill(live_data={})
    assert skill.match("play some music") is None


def test_handle_with_data():
    data = {"temperature": 65.0, "fan_duty": 40.0, "cpu_percent": 22.0}
    skill = SystemStatusSkill(live_data=data)
    m = skill.match("status report")
    result = skill.handle("status report", m, _bus())
    assert "65.0" in result
    assert "40" in result
    assert "22" in result


def test_handle_no_data():
    skill = SystemStatusSkill(live_data={})
    m = skill.match("status report")
    result = skill.handle("status report", m, _bus())
    assert isinstance(result, str)
