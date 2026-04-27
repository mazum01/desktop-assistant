"""Unit tests for src/audio/output.py — patches the module-level `sd`
attribute directly so test ordering is irrelevant."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.audio import output as audio_output
from src.audio.output import AudioOutput, AudioOutputConfig, find_output_device


_FAKE_DEVICES = [
    {"name": "bcm2835 Headphones", "max_output_channels": 2, "max_input_channels": 0},
    {"name": "USB PnP Sound Device: Sabrent Audio (hw:2,0)",
     "max_output_channels": 2, "max_input_channels": 1},
    {"name": "default", "max_output_channels": 2, "max_input_channels": 2},
]


@pytest.fixture
def fake_sd(monkeypatch):
    """Replace the module-level `sd` and `_SD_AVAILABLE` so every test
    sees a known fake regardless of suite ordering."""
    played = []

    def query_devices(idx=None, kind=None):
        return _FAKE_DEVICES if idx is None else _FAKE_DEVICES[idx]

    def play(samples, samplerate=None, device=None):
        played.append({"n_samples": len(samples),
                       "samplerate": samplerate, "device": device})

    fake = SimpleNamespace(
        query_devices=query_devices,
        play=play,
        wait=lambda: None,
        stop=lambda: None,
    )
    monkeypatch.setattr(audio_output, "sd", fake)
    monkeypatch.setattr(audio_output, "_SD_AVAILABLE", True)
    fake.played = played
    return fake


class TestFindOutputDevice:
    def test_finds_sabrent(self, fake_sd):
        assert find_output_device("Sabrent") == 1

    def test_case_insensitive(self, fake_sd):
        assert find_output_device("sabrent") == 1

    def test_returns_none_when_not_found(self, fake_sd):
        assert find_output_device("Nonexistent") is None


class TestAudioOutput:
    def test_hardware_ready_with_sabrent(self, fake_sd):
        out = AudioOutput()
        assert out.hardware_ready is True
        assert out.device_index == 1

    def test_sim_mode_when_device_missing(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(device_name="Nope"))
        assert out.hardware_ready is False

    def test_explicit_device_index(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(device_index=2))
        assert out.device_index == 2

    def test_play_sends_to_correct_device(self, fake_sd):
        out = AudioOutput()
        samples = np.zeros(1000, dtype=np.float32)
        out.play(samples, sample_rate=48000)
        assert len(fake_sd.played) == 1
        assert fake_sd.played[0]["device"] == 1
        assert fake_sd.played[0]["samplerate"] == 48000

    def test_beep_generates_correct_length(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(channels=1))
        out.beep(frequency=440, duration=0.1)
        assert fake_sd.played[0]["n_samples"] == 4800  # 0.1 s @ 48 kHz

    def test_sim_play_does_not_crash(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(device_name="Nope"))
        out.play(np.zeros(100, dtype=np.float32))
        assert fake_sd.played == []
