"""Unit tests for src/audio/input.py — patches module-level `sd`."""

from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from src.audio import input as audio_input
from src.audio.input import AudioInput, AudioInputConfig, find_input_device


_FAKE_DEVICES = [
    {"name": "bcm2835 Headphones", "max_output_channels": 2, "max_input_channels": 0},
    {"name": "USB PnP Sound Device: Sabrent Audio",
     "max_output_channels": 2, "max_input_channels": 1},
    {"name": "I2S MEMS Mic", "max_output_channels": 0, "max_input_channels": 2},
]


@pytest.fixture
def fake_sd(monkeypatch):
    recorded = []

    def query_devices(idx=None, kind=None):
        return _FAKE_DEVICES if idx is None else _FAKE_DEVICES[idx]

    def rec(n_samples, samplerate=None, channels=1, device=None, dtype="float32"):
        recorded.append({"n_samples": n_samples, "samplerate": samplerate,
                         "channels": channels, "device": device})
        return np.zeros((n_samples, channels), dtype=np.float32)

    fake = SimpleNamespace(
        query_devices=query_devices,
        rec=rec,
        wait=lambda: None,
    )
    monkeypatch.setattr(audio_input, "sd", fake)
    monkeypatch.setattr(audio_input, "_SD_AVAILABLE", True)
    fake.recorded = recorded
    return fake


class TestFindInputDevice:
    def test_finds_mems(self, fake_sd):
        assert find_input_device("MEMS") == 2

    def test_finds_sabrent_input(self, fake_sd):
        assert find_input_device("Sabrent") == 1

    def test_empty_returns_none(self, fake_sd):
        assert find_input_device("") is None

    def test_no_match_returns_none(self, fake_sd):
        assert find_input_device("nothing") is None


class TestAudioInput:
    def test_default_uses_system_default(self, fake_sd):
        mic = AudioInput()
        assert mic.hardware_ready is True
        assert mic.device_index is None

    def test_explicit_index(self, fake_sd):
        mic = AudioInput(AudioInputConfig(device_index=2))
        assert mic.device_index == 2

    def test_sim_when_named_device_missing(self, fake_sd):
        mic = AudioInput(AudioInputConfig(device_name="NoSuchMic"))
        assert mic.hardware_ready is False

    def test_record_returns_correct_length(self, fake_sd):
        mic = AudioInput(AudioInputConfig(sample_rate=16000))
        samples = mic.record(seconds=1.0)
        assert samples.shape == (16000,)
        assert samples.dtype == np.float32

    def test_record_multichannel(self, fake_sd):
        mic = AudioInput(AudioInputConfig(channels=2, sample_rate=16000))
        samples = mic.record(seconds=0.5)
        assert samples.shape == (8000, 2)

    def test_sim_record_returns_silence(self, fake_sd):
        mic = AudioInput(AudioInputConfig(device_name="NoSuchMic", sample_rate=16000))
        samples = mic.record(seconds=0.5)
        assert samples.shape == (8000,)
        assert np.all(samples == 0.0)
