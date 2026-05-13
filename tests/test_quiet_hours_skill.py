"""Tests for QuietHoursSkill."""
from unittest.mock import MagicMock

from src.skills.quiet_hours_skill import QuietHoursSkill


def _bus():
    return MagicMock()


def _mock_qh(enabled=False, start="22:00", end="07:00"):
    qh = MagicMock()
    qh.enabled = enabled
    qh.start = start
    qh.end = end
    return qh


def test_matches_enable_quiet_hours():
    assert QuietHoursSkill().match("enable quiet hours") is not None


def test_matches_disable_quiet_hours():
    assert QuietHoursSkill().match("disable quiet hours") is not None


def test_matches_turn_on_quiet_mode():
    assert QuietHoursSkill().match("turn on quiet mode") is not None


def test_matches_status_query():
    assert QuietHoursSkill().match("are we in quiet mode") is not None


def test_no_match():
    assert QuietHoursSkill().match("what time is it") is None


def test_handle_no_qh_configured():
    skill = QuietHoursSkill(quiet_hours=None)
    m = skill.match("enable quiet hours")
    result = skill.handle("enable quiet hours", m, _bus())
    assert "not configured" in result.lower()


def test_handle_enable():
    qh = _mock_qh(enabled=False)
    skill = QuietHoursSkill(quiet_hours=qh)
    bus = _bus()
    m = skill.match("enable quiet hours")
    result = skill.handle("enable quiet hours", m, bus)
    qh.update.assert_called_once()
    bus.publish.assert_called_once()
    assert isinstance(result, str)


def test_handle_disable():
    qh = _mock_qh(enabled=True)
    skill = QuietHoursSkill(quiet_hours=qh)
    bus = _bus()
    m = skill.match("disable quiet hours")
    result = skill.handle("disable quiet hours", m, bus)
    qh.update.assert_called_once()
    assert isinstance(result, str)


def test_handle_status_query():
    qh = _mock_qh(enabled=True)
    skill = QuietHoursSkill(quiet_hours=qh)
    m = skill.match("are we in quiet mode")
    result = skill.handle("are we in quiet mode", m, _bus())
    assert isinstance(result, str)
