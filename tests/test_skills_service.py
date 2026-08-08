"""Integration tests for SkillsService dispatch."""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest

from src.skills.base import SkillRegistry, Skill
import re


# ---------------------------------------------------------------------------
# SkillRegistry unit tests
# ---------------------------------------------------------------------------

def make_skill(name, pattern_str, response):
    """Create a minimal test skill."""
    class _TestSkill(Skill):
        @property
        def patterns(self):
            return [re.compile(pattern_str)]
        def handle(self, text, match, bus):
            return response
    s = _TestSkill()
    s.name = name
    return s


def test_registry_first_match_wins():
    reg = SkillRegistry()
    reg.register(make_skill("a", r"hello", "response_a"))
    reg.register(make_skill("b", r"hello", "response_b"))
    bus = MagicMock()
    matched = reg.dispatch("hello", bus)
    assert matched is True
    bus.publish.assert_called_once_with("av.say", {"text": "response_a"})


def test_registry_no_match():
    reg = SkillRegistry()
    reg.register(make_skill("a", r"hello", "hi"))
    bus = MagicMock()
    matched = reg.dispatch("goodbye world", bus)
    assert matched is False
    bus.publish.assert_not_called()


def test_registry_none_response_no_say():
    """If handle() returns None, av.say must NOT be published."""
    class SilentSkill(Skill):
        name = "silent"
        @property
        def patterns(self):
            return [re.compile(r"describe")]
        def handle(self, text, match, bus):
            bus.publish("vision.describe", {})
            return None

    reg = SkillRegistry()
    reg.register(SilentSkill())
    bus = MagicMock()
    matched = reg.dispatch("describe the scene", bus)
    assert matched is True
    bus.publish.assert_called_once_with("vision.describe", {})


def test_registry_exception_does_not_propagate():
    """A skill that raises must not crash the registry."""
    class BrokenSkill(Skill):
        name = "broken"
        @property
        def patterns(self):
            return [re.compile(r"broken")]
        def handle(self, text, match, bus):
            raise RuntimeError("unexpected")

    reg = SkillRegistry()
    reg.register(BrokenSkill())
    bus = MagicMock()
    # Should not raise:
    matched = reg.dispatch("broken skill", bus)
    assert matched is True  # skill did match


def test_registry_skill_names():
    reg = SkillRegistry()
    reg.register(make_skill("alpha", r"a", "A"))
    reg.register(make_skill("beta",  r"b", "B"))
    assert reg.skill_names == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# SkillsService lifecycle
# ---------------------------------------------------------------------------

def test_skills_service_subscribes_and_dispatches():
    """SkillsService must subscribe to av.utterance on on_start()."""
    from src.services.skills_service import SkillsService

    bus = MagicMock()
    svc = SkillsService(bus)
    assert svc.name == "skills"
    svc.on_start()
    bus.subscribe.assert_any_call("av.utterance", svc._on_utterance)


def test_skills_service_dispatches_on_utterance():
    """_on_utterance with a matching phrase must trigger bus.publish."""
    from src.services.skills_service import SkillsService

    bus = MagicMock()
    svc = SkillsService(bus)

    # Call _on_utterance directly (bypass bus subscription for simplicity)
    svc._on_utterance("av.utterance", {"text": "what time is it"})
    # TellTimeSkill returns a string → av.say published on the bus argument
    published_topics = [c[0][0] for c in bus.publish.call_args_list]
    assert "av.say" in published_topics


def test_skills_service_ignores_empty_utterance():
    from src.services.skills_service import SkillsService

    bus = MagicMock()
    svc = SkillsService(bus)
    svc._on_utterance("av.utterance", {"text": ""})
    svc._on_utterance("av.utterance", {})
    bus.publish.assert_not_called()


def test_skills_service_registers_anthropic_toggle_skill():
    from src.services.skills_service import SkillsService

    bus = MagicMock()
    svc = SkillsService(bus)
    assert "anthropic_toggle" in svc._registry.skill_names


def test_skills_service_dispatches_anthropic_toggle():
    from src.services.skills_service import SkillsService

    bus = MagicMock()
    svc = SkillsService(bus)
    svc._on_utterance("av.utterance", {"text": "disable anthropic"})
    published_topics = [c[0][0] for c in bus.publish.call_args_list]
    assert "anthropic.set_enabled" in published_topics
