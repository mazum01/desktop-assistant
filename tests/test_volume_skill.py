"""Tests for VolumeSkill."""
from unittest.mock import MagicMock

from src.skills.volume_skill import VolumeSkill


def _bus():
    return MagicMock()


def test_matches_set_volume():
    assert VolumeSkill().match("set volume to 60") is not None


def test_matches_mute():
    assert VolumeSkill().match("mute the music") is not None


def test_matches_louder():
    assert VolumeSkill().match("louder") is not None


def test_matches_volume_up():
    assert VolumeSkill().match("volume up") is not None


def test_matches_max_volume():
    assert VolumeSkill().match("maximum volume") is not None


def test_no_match():
    assert VolumeSkill().match("what time is it") is None


def test_mute_publishes_level_zero():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("mute the music")
    skill.handle("mute the music", m, bus)
    bus.publish.assert_called_once_with("music.set_volume", {"level": 0})


def test_max_volume_publishes_level_100():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("maximum volume")
    skill.handle("maximum volume", m, bus)
    bus.publish.assert_called_once_with("music.set_volume", {"level": 100})


def test_set_absolute_volume():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("set volume to 70")
    skill.handle("set volume to 70", m, bus)
    bus.publish.assert_called_once_with("music.set_volume", {"level": 70})


def test_relative_up_publishes_delta():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("louder")
    skill.handle("louder", m, bus)
    call_args = bus.publish.call_args[0]
    assert call_args[0] == "music.set_volume"
    assert call_args[1].get("delta", 0) > 0


def test_relative_down_publishes_negative_delta():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("quieter")
    skill.handle("quieter", m, bus)
    call_args = bus.publish.call_args[0]
    assert call_args[0] == "music.set_volume"
    assert call_args[1].get("delta", 0) < 0


def test_volume_clamped_to_100():
    bus = _bus()
    skill = VolumeSkill()
    m = skill.match("set volume to 999")
    skill.handle("set volume to 999", m, bus)
    bus.publish.assert_called_once_with("music.set_volume", {"level": 100})
