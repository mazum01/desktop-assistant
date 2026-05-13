"""Tests for HelpSkill."""
from unittest.mock import MagicMock

from src.skills.help_skill import HelpSkill
from src.skills.base import SkillRegistry


def _bus():
    return MagicMock()


def _registry():
    reg = SkillRegistry()
    reg.register(HelpSkill())
    return reg


def test_help_skill_matches_help():
    assert HelpSkill().match("help") is not None


def test_help_skill_matches_what_can_you_do():
    assert HelpSkill().match("what can you do") is not None


def test_help_skill_matches_list_skills():
    assert HelpSkill().match("list skills") is not None


def test_help_skill_no_match():
    assert HelpSkill().match("play some music") is None


def test_help_skill_handle_returns_string():
    skill = HelpSkill()
    m = skill.match("help")
    result = skill.handle("help", m, _bus())
    assert isinstance(result, str) and len(result) > 0


def test_help_skill_registered_in_registry():
    result = _registry().dispatch("help", _bus())
    assert result is not None
