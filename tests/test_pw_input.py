"""Tests for src/audio/pw_input.py — PipeWire-native mic capture."""

import numpy as np
import pytest

import src.audio.pw_input as pw_input
from src.audio.pw_input import PipeWireMicInput, PipeWireMicConfig


def test_sim_mode_when_pw_record_missing(monkeypatch):
    """When pw-record is unavailable, the input runs in sim mode and returns silence."""
    monkeypatch.setattr(pw_input, "_PW_RECORD", None)
    mic = PipeWireMicInput(PipeWireMicConfig(sample_rate=16000))
    assert mic.hardware_ready is False
    out = mic.record(0.25)
    assert out.shape == (4000,)  # 0.25s * 16000
    assert np.all(out == 0.0)
    mic.close()  # must be safe even in sim mode


def test_record_returns_silence_on_timeout(monkeypatch):
    """record() returns zero-padded silence rather than blocking when no data arrives."""
    monkeypatch.setattr(pw_input, "_PW_RECORD", None)
    mic = PipeWireMicInput(PipeWireMicConfig(sample_rate=16000))
    out = mic.record(0.1)
    assert out.dtype == np.float32
    assert out.shape == (1600,)


def test_resolve_source_name_parses_pw_dump(monkeypatch):
    """_resolve_source_name finds an Audio/Source node by name substring."""
    fake_dump = (
        '[{"type":"PipeWire:Interface:Node","info":{"props":'
        '{"media.class":"Audio/Source","node.name":"alsa_input.reSpeaker_xyz"}}}]'
    )

    class _R:
        returncode = 0
        stdout = fake_dump

    monkeypatch.setattr(pw_input.subprocess, "run", lambda *a, **k: _R())
    assert pw_input._resolve_source_name("reSpeaker") == "alsa_input.reSpeaker_xyz"
    assert pw_input._resolve_source_name("nonexistent") is None


def test_resolve_source_name_empty_match():
    assert pw_input._resolve_source_name("") is None


def test_buffer_to_samples_conversion(monkeypatch):
    """A populated ring buffer is converted to correctly scaled float32 samples."""
    monkeypatch.setattr(pw_input, "_PW_RECORD", None)
    mic = PipeWireMicInput(PipeWireMicConfig(sample_rate=16000))
    # Manually mark it "ready" and feed the buffer to exercise record()'s decode path.
    mic._sim = False

    class _FakeProc:
        def poll(self):
            return None
    mic._proc = _FakeProc()  # type: ignore

    # 1600 samples (0.1s) of half-scale s16.
    pcm = np.full(1600, 16384, dtype=np.int16)
    mic._buf.extend(pcm.tobytes())
    out = mic.record(0.1)
    assert out.shape == (1600,)
    assert np.allclose(out, 16384 / 32768.0, atol=1e-4)
