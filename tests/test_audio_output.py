"""Unit tests for src/audio/output.py — patches the module-level ``_APLAY_AVAILABLE``
and ``subprocess.Popen`` so tests run without a real audio device."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch, call
import subprocess

import numpy as np
import pytest

from src.audio import output as audio_output
from src.audio.output import AudioOutput, AudioOutputConfig, find_output_device


class FakeProc:
    """Minimal stand-in for subprocess.Popen used in tests."""

    def __init__(self, *args, **kwargs):
        self.stdin = BytesIO()
        self._returncode = 0
        self._alive = True
        self.written: list[bytes] = []
        _orig_write = self.stdin.write

        def _tracked_write(data: bytes) -> int:
            self.written.append(data)
            return _orig_write(data)

        self.stdin.write = _tracked_write
        self.stdin.flush = lambda: None
        self.stdin.close = lambda: setattr(self, "_alive", False)

    def poll(self):
        return None if self._alive else self._returncode

    def wait(self, timeout=None):
        self._alive = False
        return self._returncode

    def kill(self):
        self._alive = False

    @property
    def total_written(self) -> int:
        return sum(len(b) for b in self.written)


@pytest.fixture
def fake_aplay(monkeypatch):
    """Replace _APLAY_AVAILABLE=True and Popen so tests see a known fake."""
    procs: list[FakeProc] = []

    def make_proc(*args, **kwargs):
        p = FakeProc(*args, **kwargs)
        procs.append(p)
        return p

    monkeypatch.setattr(audio_output, "_APLAY_AVAILABLE", True)
    monkeypatch.setattr(audio_output.subprocess, "Popen", make_proc)
    return procs


class TestFindOutputDevice:
    def test_always_returns_none(self):
        """find_output_device is a legacy stub — always returns None."""
        assert find_output_device("Sabrent") is None
        assert find_output_device() is None


class TestAudioOutput:
    def test_hardware_ready_when_aplay_available(self, fake_aplay):
        out = AudioOutput()
        assert out.hardware_ready is True

    def test_sim_mode_when_aplay_missing(self, monkeypatch):
        monkeypatch.setattr(audio_output, "_APLAY_AVAILABLE", False)
        out = AudioOutput()
        assert out.hardware_ready is False

    def test_device_index_always_none(self, fake_aplay):
        out = AudioOutput()
        assert out.device_index is None

    def test_play_sends_s16_data(self, fake_aplay):
        out = AudioOutput(AudioOutputConfig(loudness_boost=1.0, channels=1))
        samples = np.zeros(1000, dtype=np.float32)
        out.play(samples, sample_rate=44100)
        assert len(fake_aplay) == 1
        assert fake_aplay[0].total_written > 0

    def test_play_spawns_aplay_with_pulse_device(self, monkeypatch):
        monkeypatch.setattr(audio_output, "_APLAY_AVAILABLE", True)
        spawned_cmds: list[list] = []

        class _Proc(FakeProc):
            pass

        def mock_popen(cmd, **kw):
            spawned_cmds.append(cmd)
            return FakeProc()

        monkeypatch.setattr(audio_output.subprocess, "Popen", mock_popen)
        out = AudioOutput()
        out.play(np.zeros(100, dtype=np.float32))
        assert any("aplay" in c[0] for c in spawned_cmds)
        assert any("-D" in c and "pulse" in c for c in spawned_cmds)

    def test_beep_generates_audio(self, fake_aplay):
        out = AudioOutput(AudioOutputConfig(channels=1))
        out.beep(frequency=440, duration=0.1)
        assert len(fake_aplay) == 1
        # 0.1s @ 44100 Hz, 1 ch, 2 bytes/sample → 8820 bytes
        assert fake_aplay[0].total_written >= 8820

    def test_sim_play_does_not_crash(self, monkeypatch):
        monkeypatch.setattr(audio_output, "_APLAY_AVAILABLE", False)
        out = AudioOutput()
        out.play(np.zeros(100, dtype=np.float32))
        assert len([]) == 0  # no procs spawned

    def test_write_chunk_reuses_proc(self, fake_aplay):
        out = AudioOutput()
        chunk = np.ones(2400, dtype=np.float32) * 0.5
        out.write_chunk(chunk, sample_rate=44100)
        out.write_chunk(chunk, sample_rate=44100)
        assert len(fake_aplay) == 1  # same proc reused

    def test_flush_closes_proc(self, fake_aplay):
        out = AudioOutput()
        out.write_chunk(np.zeros(100, dtype=np.float32))
        assert out._proc is not None
        out.flush()
        assert out._proc is None

    def test_close_terminates_proc(self, fake_aplay):
        out = AudioOutput()
        out.write_chunk(np.zeros(100, dtype=np.float32))
        out.close()
        assert out._proc is None

    def test_stereo_upmix_from_mono(self, fake_aplay):
        cfg = AudioOutputConfig(channels=2, loudness_boost=1.0)
        out = AudioOutput(cfg)
        # mono 1-D array
        samples = np.ones(1000, dtype=np.float32) * 0.5
        out.write_chunk(samples, sample_rate=44100)
        proc = fake_aplay[0]
        # S16_LE stereo: 1000 frames × 2 ch × 2 bytes = 4000 bytes
        assert proc.total_written == 4000

