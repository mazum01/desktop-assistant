"""Tests for src.audio.volume_state — persisted output volume."""

import pytest

from src.audio import volume_state


@pytest.fixture
def state_file(tmp_path, monkeypatch):
    path = tmp_path / "music_volume.txt"
    monkeypatch.setattr(volume_state, "STATE_DIR", tmp_path)
    monkeypatch.setattr(volume_state, "VOLUME_FILE", path)
    return path


def test_load_volume_missing_returns_none(state_file):
    assert volume_state.load_volume() is None


def test_save_then_load_roundtrip(state_file):
    volume_state.save_volume(30)
    assert state_file.read_text() == "30"
    assert volume_state.load_volume() == 30


def test_save_volume_clamps_out_of_range(state_file):
    volume_state.save_volume(150)
    assert volume_state.load_volume() == 100
    volume_state.save_volume(-5)
    assert volume_state.load_volume() == 0


def test_load_volume_rejects_garbage(state_file):
    state_file.write_text("not-a-number")
    assert volume_state.load_volume() is None


def test_load_volume_rejects_out_of_range_file(state_file):
    state_file.write_text("420")
    assert volume_state.load_volume() is None


def test_load_scalar_uses_default_when_unset(state_file):
    assert volume_state.load_scalar(1.0) == 1.0


def test_load_scalar_converts_percent(state_file):
    volume_state.save_volume(30)
    assert volume_state.load_scalar(1.0) == pytest.approx(0.30)
