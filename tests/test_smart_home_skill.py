"""Tests for SmartHomeSkill."""
from unittest.mock import MagicMock

from src.skills.smart_home_skill import SmartHomeSkill


def _bus():
    return MagicMock()


def test_disabled_by_default():
    assert SmartHomeSkill().enabled is False


def test_matches_turn_on():
    assert SmartHomeSkill().match("turn on the living room lights") is not None


def test_matches_turn_off():
    assert SmartHomeSkill().match("turn off the bedroom light") is not None


def test_matches_thermostat():
    assert SmartHomeSkill().match("set thermostat to 72") is not None


def test_matches_lock():
    assert SmartHomeSkill().match("lock the front door") is not None


def test_no_match():
    assert SmartHomeSkill().match("what time is it") is None


def test_handle_not_configured():
    skill = SmartHomeSkill()
    skill.enabled = True
    m = skill.match("turn on the lights")
    result = skill.handle("turn on the lights", m, _bus())
    assert "not configured" in result.lower()


def test_config_schema_fields():
    schema = SmartHomeSkill().config_schema
    names = [f.name for f in schema]
    assert "base_url" in names
    assert "token" in names
    assert "default_room" in names


def test_token_is_secret():
    schema = SmartHomeSkill().config_schema
    token_field = next(f for f in schema if f.name == "token")
    assert token_field.secret is True


def test_set_config_base_url():
    skill = SmartHomeSkill()
    skill.set_config("base_url", "http://ha.local:8123")
    assert skill._base_url == "http://ha.local:8123"


def test_set_config_token():
    skill = SmartHomeSkill()
    skill.set_config("token", "mytoken123")
    assert skill._token == "mytoken123"


def test_get_config_masks_token():
    skill = SmartHomeSkill()
    skill.set_config("token", "secrettoken")
    cfg = skill.get_config()
    assert cfg["token"] == "****"


def test_get_config_empty_token():
    skill = SmartHomeSkill()
    cfg = skill.get_config()
    assert cfg["token"] == ""
