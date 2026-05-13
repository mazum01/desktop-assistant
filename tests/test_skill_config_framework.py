"""Tests for the Skill config framework (ConfigField, enabled, dispatch skip)."""
from unittest.mock import MagicMock
import re

from src.skills.base import ConfigField, Skill, SkillRegistry


# ---------------------------------------------------------------------------
# Minimal concrete skill for testing
# ---------------------------------------------------------------------------

class _EchoSkill(Skill):
    name = "echo"

    @property
    def patterns(self):
        return [re.compile(r"\becho\b")]

    def handle(self, text, match, bus):
        return f"echo: {text}"

    @property
    def config_schema(self):
        return [
            ConfigField("volume", "Volume", "int", 50, min=0, max=100),
            ConfigField("prefix", "Prefix", "str", ""),
        ]

    def get_config(self):
        return {"volume": self._vol, "prefix": self._prefix}

    def set_config(self, key, value):
        if key == "volume":
            v = int(value)
            if not 0 <= v <= 100:
                raise ValueError("volume must be 0–100")
            self._vol = v
        elif key == "prefix":
            self._prefix = str(value)
        else:
            raise ValueError(f"unknown field {key!r}")

    def __init__(self):
        super().__init__()
        self._vol = 50
        self._prefix = ""


def _bus():
    return MagicMock()


# ---------------------------------------------------------------------------
# ConfigField
# ---------------------------------------------------------------------------

def test_config_field_as_dict():
    f = ConfigField("loc", "Location", "str", "auto", description="City or auto")
    d = f.as_dict()
    assert d["name"] == "loc"
    assert d["type"] == "str"
    assert d["default"] == "auto"
    assert d["description"] == "City or auto"
    assert d["secret"] is False


def test_config_field_select():
    f = ConfigField("units", "Units", "select", "imperial", options=["imperial", "metric"])
    assert "imperial" in f.options


def test_config_field_secret():
    f = ConfigField("token", "Token", "str", "", secret=True)
    assert f.as_dict()["secret"] is True


# ---------------------------------------------------------------------------
# Skill.enabled flag
# ---------------------------------------------------------------------------

def test_skill_enabled_by_default():
    skill = _EchoSkill()
    assert skill.enabled is True


def test_skill_can_be_disabled():
    skill = _EchoSkill()
    skill.enabled = False
    assert skill.enabled is False


# ---------------------------------------------------------------------------
# Skill config interface
# ---------------------------------------------------------------------------

def test_skill_get_config_defaults():
    skill = _EchoSkill()
    cfg = skill.get_config()
    assert cfg["volume"] == 50
    assert cfg["prefix"] == ""


def test_skill_set_config_valid():
    skill = _EchoSkill()
    skill.set_config("volume", 75)
    assert skill.get_config()["volume"] == 75


def test_skill_set_config_invalid_raises():
    skill = _EchoSkill()
    try:
        skill.set_config("volume", 999)
        assert False, "should raise"
    except ValueError:
        pass


def test_skill_set_config_unknown_raises():
    skill = _EchoSkill()
    try:
        skill.set_config("nonexistent", "x")
        assert False, "should raise"
    except ValueError:
        pass


def test_skill_config_schema_returns_fields():
    skill = _EchoSkill()
    schema = skill.config_schema
    assert len(schema) == 2
    names = [f.name for f in schema]
    assert "volume" in names
    assert "prefix" in names


# ---------------------------------------------------------------------------
# Skills with no config (default ABC stubs)
# ---------------------------------------------------------------------------

def test_default_skill_config_schema_empty():
    from src.skills.greeting import GreetingSkill
    assert GreetingSkill().config_schema == []


def test_default_skill_get_config_empty():
    from src.skills.greeting import GreetingSkill
    assert GreetingSkill().get_config() == {}


# ---------------------------------------------------------------------------
# SkillRegistry.dispatch skips disabled skills
# ---------------------------------------------------------------------------

def test_registry_skips_disabled_skill():
    reg = SkillRegistry()
    skill = _EchoSkill()
    skill.enabled = False
    reg.register(skill)
    bus = _bus()
    matched = reg.dispatch("echo this", bus)
    assert matched is False
    bus.publish.assert_not_called()


def test_registry_dispatches_enabled_skill():
    reg = SkillRegistry()
    reg.register(_EchoSkill())
    bus = _bus()
    matched = reg.dispatch("echo this", bus)
    assert matched is True
    bus.publish.assert_called_once()


def test_registry_find_returns_skill():
    reg = SkillRegistry()
    skill = _EchoSkill()
    reg.register(skill)
    assert reg.find("echo") is skill


def test_registry_find_unknown_returns_none():
    reg = SkillRegistry()
    assert reg.find("nonexistent") is None


def test_registry_dispatch_prefers_first_enabled():
    """First skill disabled, second enabled — second should fire."""
    class _A(Skill):
        name = "a"
        @property
        def patterns(self): return [re.compile(r"\btest\b")]
        def handle(self, t, m, b): return "from A"

    class _B(Skill):
        name = "b"
        @property
        def patterns(self): return [re.compile(r"\btest\b")]
        def handle(self, t, m, b): return "from B"

    reg = SkillRegistry()
    a = _A(); a.enabled = False
    b = _B()
    reg.register(a)
    reg.register(b)
    bus = _bus()
    reg.dispatch("test", bus)
    call_args = bus.publish.call_args[0]
    assert call_args[1]["text"] == "from B"
