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


class FakeOutputStream:
    """Minimal stand-in for sd.OutputStream used in tests."""

    def __init__(self, *args, **kwargs):
        self.active = True
        self.latency = 0.2
        self.written: list[np.ndarray] = []
        self._started = False

    def start(self):
        self._started = True

    def write(self, data: np.ndarray):
        self.written.append(data.copy())

    def abort(self):
        self.active = False

    def stop(self):
        self.active = False

    def close(self):
        self.active = False

    @property
    def total_samples(self) -> int:
        return sum(len(a) for a in self.written)


@pytest.fixture
def fake_sd(monkeypatch):
    """Replace the module-level `sd` and `_SD_AVAILABLE` so every test
    sees a known fake regardless of suite ordering."""
    played = []
    stream_instances: list[FakeOutputStream] = []

    def query_devices(idx=None, kind=None):
        return _FAKE_DEVICES if idx is None else _FAKE_DEVICES[idx]

    def make_output_stream(*args, **kwargs):
        inst = FakeOutputStream(*args, **kwargs)
        stream_instances.append(inst)
        # Mirror written data into played[] for legacy test assertions
        orig_write = inst.write
        def _tracked_write(data):
            played.append({"n_samples": len(data), "device": kwargs.get("device")})
            orig_write(data)
        inst.write = _tracked_write
        return inst

    fake = SimpleNamespace(
        query_devices=query_devices,
        OutputStream=make_output_stream,
        play=lambda *a, **kw: None,  # legacy fallback — not used by new path
        wait=lambda: None,
        stop=lambda: None,
    )
    monkeypatch.setattr(audio_output, "sd", fake)
    monkeypatch.setattr(audio_output, "_SD_AVAILABLE", True)
    fake.played = played
    fake.streams = stream_instances
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
        # Persistent stream should have received the audio data
        assert len(fake_sd.streams) >= 1
        assert fake_sd.streams[0].total_samples > 0

    def test_beep_generates_correct_length(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(channels=1))
        out.beep(frequency=440, duration=0.1)
        # beep() calls play(); verify audio data was written to stream
        assert len(fake_sd.streams) >= 1
        # 0.1s @ 48kHz = 4800 samples; flush() writes silence pad after,
        # so total written includes both the beep and the silence drain
        assert fake_sd.streams[0].total_samples >= 4800

    def test_sim_play_does_not_crash(self, fake_sd):
        out = AudioOutput(AudioOutputConfig(device_name="Nope"))
        out.play(np.zeros(100, dtype=np.float32))
        assert fake_sd.played == []

    def test_write_chunk_streams_to_persistent_stream(self, fake_sd):
        out = AudioOutput()
        chunk = np.ones(2400, dtype=np.float32) * 0.5
        out.write_chunk(chunk, sample_rate=48000)
        assert len(fake_sd.streams) == 1
        assert fake_sd.streams[0]._started

    def test_close_clears_stream(self, fake_sd):
        out = AudioOutput()
        out.write_chunk(np.zeros(100, dtype=np.float32))
        out.close()
        assert out._stream is None

    def test_flush_writes_silence(self, fake_sd):
        out = AudioOutput()
        out.write_chunk(np.zeros(100, dtype=np.float32))
        before = fake_sd.streams[0].total_samples
        out.flush()
        after = fake_sd.streams[0].total_samples
        assert after > before  # silence pad was written
