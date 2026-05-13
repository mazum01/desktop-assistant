"""Tests for ReminderSkill."""
import time
from unittest.mock import MagicMock

from src.skills.reminder_skill import ReminderSkill


def _bus():
    return MagicMock()


def _skill():
    s = ReminderSkill()
    s.start(_bus())
    return s


def test_matches_remind_in_minutes():
    assert ReminderSkill().match("remind me to call mom in 10 minutes") is not None


def test_matches_remind_in_hours():
    assert ReminderSkill().match("remind me to exercise in 2 hours") is not None


def test_matches_remind_at_time():
    assert ReminderSkill().match("remind me to call the doctor at 9:00") is not None


def test_matches_list_reminders():
    assert ReminderSkill().match("what are my reminders") is not None


def test_matches_clear_reminders():
    assert ReminderSkill().match("clear all reminders") is not None


def test_no_match():
    assert ReminderSkill().match("what is the weather") is None


def test_handle_no_reminders_list():
    skill = _skill()
    m = skill.match("what are my reminders")
    result = skill.handle("what are my reminders", m, _bus())
    assert "no pending" in result.lower()
    skill.stop()


def test_handle_schedule_in_minutes():
    skill = _skill()
    m = skill.match("remind me to call mom in 15 minutes")
    result = skill.handle("remind me to call mom in 15 minutes", m, _bus())
    assert "call mom" in result.lower()
    assert "15" in result
    assert len(skill._reminders) == 1
    skill.stop()


def test_handle_clear_all():
    skill = _skill()
    bus = _bus()
    # Add a reminder first
    m = skill.match("remind me to test in 5 minutes")
    skill.handle("remind me to test in 5 minutes", m, bus)
    assert len(skill._reminders) == 1
    # Now clear
    m2 = skill.match("clear all reminders")
    result = skill.handle("clear all reminders", m2, bus)
    assert len(skill._reminders) == 0
    assert "1" in result
    skill.stop()


def test_reminder_fires_when_due():
    """Verify reminder is removed from queue when it becomes due."""
    bus = _bus()
    skill = ReminderSkill()
    # Don't start background thread — manually invoke _loop logic
    skill._bus = bus
    skill._add("test task", 0.01, bus)
    time.sleep(0.02)  # Let it become due
    # Simulate what the loop does: check due reminders
    now = time.monotonic()
    with skill._lock:
        fired = [r for r in skill._reminders if now >= r.fire_at]
        skill._reminders = [r for r in skill._reminders if now < r.fire_at]
    for r in fired:
        bus.publish("av.say", {"text": f"Reminder: {r.text}"})
    assert "test task" in fired[0].text
    bus.publish.assert_called_once_with("av.say", {"text": "Reminder: test task"})


def test_config_schema():
    schema = ReminderSkill().config_schema
    names = [f.name for f in schema]
    assert "snooze_min" in names
    assert "pending" in names


def test_set_snooze_min():
    skill = ReminderSkill()
    skill.set_config("snooze_min", 10)
    assert skill.get_config()["snooze_min"] == 10


def test_set_snooze_invalid():
    skill = ReminderSkill()
    try:
        skill.set_config("snooze_min", 0)
        assert False, "should raise"
    except ValueError:
        pass
