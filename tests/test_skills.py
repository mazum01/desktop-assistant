"""Tests for individual skill classes (pattern matching + handle() logic)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.skills.describe_scene import DescribeSceneSkill
from src.skills.face_tracking_toggle import FaceTrackingToggleSkill
from src.skills.greeting import GreetingSkill
from src.skills.meet_face import MeetFaceSkill
from src.skills.motion_control import MotionControlSkill
from src.skills.music_control import MusicControlSkill
from src.skills.object_detect_toggle import ObjectDetectToggleSkill
from src.skills.tell_joke import TellJokeSkill
from src.skills.tell_time import TellTimeSkill


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_bus():
    return MagicMock()


# ---------------------------------------------------------------------------
# DescribeSceneSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what do you see",
    "describe the scene",
    "what is in front",
    "look around",
    "tell me what you see",
    "What can you see right now",
])
def test_describe_scene_matches(text):
    assert DescribeSceneSkill().match(text) is not None


def test_describe_scene_publishes(monkeypatch):
    bus = make_bus()
    skill = DescribeSceneSkill()
    m = skill.match("what do you see")
    result = skill.handle("what do you see", m, bus)
    bus.publish.assert_called_once_with("vision.describe", {})
    assert result is None


# ---------------------------------------------------------------------------
# TellTimeSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "what time is it",
    "what's the time",
    "tell me the time",
    "current time",
])
def test_tell_time_matches(text):
    assert TellTimeSkill().match(text) is not None


def test_tell_time_returns_string():
    skill = TellTimeSkill()
    m = skill.match("what time is it")
    result = skill.handle("what time is it", m, make_bus())
    assert isinstance(result, str)
    assert "M" in result  # AM or PM


# ---------------------------------------------------------------------------
# TellJokeSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "tell me a joke",
    "say a joke",
    "tell me a dad joke",
    "dad joke",
    "make me laugh",
    "give me a funny joke",
])
def test_tell_joke_matches(text):
    assert TellJokeSkill().match(text) is not None


def test_tell_joke_publishes(monkeypatch):
    bus = make_bus()
    skill = TellJokeSkill()
    m = skill.match("tell me a joke")
    result = skill.handle("tell me a joke", m, bus)
    bus.publish.assert_called_once_with("av.tell_joke", {})
    assert result is None


# ---------------------------------------------------------------------------
# GreetingSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "hello",
    "hi there",
    "hey",
    "good morning",
    "good evening",
    "howdy",
    "greetings",
])
def test_greeting_matches(text):
    assert GreetingSkill().match(text) is not None


def test_greeting_no_match():
    assert GreetingSkill().match("turn off the lights") is None


def test_greeting_returns_string():
    skill = GreetingSkill()
    m = skill.match("hello")
    result = skill.handle("hello", m, make_bus())
    assert isinstance(result, str) and len(result) > 0


# ---------------------------------------------------------------------------
# MeetFaceSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_name", [
    ("my name is Alice", "Alice"),
    ("call me Bob", "Bob"),
    ("I'm Charlie", "Charlie"),
    ("name me Dave", "Dave"),
])
def test_meet_face_matches(text, expected_name):
    skill = MeetFaceSkill()
    m = skill.match(text)
    assert m is not None
    assert m.group("name").strip().title() == expected_name


def test_meet_face_publishes():
    bus = make_bus()
    skill = MeetFaceSkill()
    m = skill.match("my name is Alice")
    result = skill.handle("my name is Alice", m, bus)
    bus.publish.assert_called_once_with("face.meet", {"name": "Alice"})
    assert result is None


# ---------------------------------------------------------------------------
# MotionControlSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_angle", [
    ("look left",      145.0),
    ("look to the right", 215.0),
    ("look ahead",     180.0),
    ("face center",    180.0),
    ("turn left",      145.0),
    ("pan right",      215.0),
])
def test_motion_control(text, expected_angle):
    bus = make_bus()
    skill = MotionControlSkill()
    m = skill.match(text)
    assert m is not None
    skill.handle(text, m, bus)
    bus.publish.assert_called_once()
    _, payload = bus.publish.call_args[0]
    assert payload["angle"] == pytest.approx(expected_angle)


# ---------------------------------------------------------------------------
# MusicControlSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_topic", [
    ("play the music",          "music.play"),
    ("start music",             "music.play"),
    ("stop the music",          "music.stop"),
    ("pause music",             "music.pause"),
    ("skip song",               "music.next"),
    ("next track",              "music.next"),
    ("thumbs up",               "music.thumbs_up"),
    ("thumbs down",             "music.thumbs_down"),
    ("I like this song",        "music.thumbs_up"),
    ("dislike this track",      "music.thumbs_down"),
])
def test_music_control(text, expected_topic):
    bus = make_bus()
    skill = MusicControlSkill()
    m = skill.match(text)
    assert m is not None, f"no match for {text!r}"
    skill.handle(text, m, bus)
    bus.publish.assert_called_once()
    topic = bus.publish.call_args[0][0]
    assert topic == expected_topic


# ---------------------------------------------------------------------------
# ObjectDetectToggleSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_enabled", [
    ("enable object detection", True),
    ("turn on objects",         True),
    ("disable object detection",False),
    ("turn off objects",        False),
    ("stop object detection",   False),
])
def test_object_detect_toggle(text, expected_enabled):
    bus = make_bus()
    skill = ObjectDetectToggleSkill()
    m = skill.match(text)
    assert m is not None
    skill.handle(text, m, bus)
    bus.publish.assert_called_once_with("object.set_enabled", {"enabled": expected_enabled})


# ---------------------------------------------------------------------------
# FaceTrackingToggleSkill
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,expected_enabled", [
    ("follow me",                   True),
    ("enable face tracking",        True),
    ("start face tracking",         True),
    ("track my face",               True),
    ("stop following me",           False),
    ("don't follow me",             False),
    ("disable face tracking",       False),
    ("stop face tracking",          False),
])
def test_face_tracking_toggle(text, expected_enabled):
    bus = make_bus()
    skill = FaceTrackingToggleSkill()
    m = skill.match(text)
    assert m is not None, f"no match for {text!r}"
    skill.handle(text, m, bus)
    bus.publish.assert_called_once_with(
        "tracking.set_face_tracking", {"enabled": expected_enabled}
    )
