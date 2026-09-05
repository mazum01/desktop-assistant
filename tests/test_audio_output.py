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



def test_custom_eq_boosts_rather_than_attenuates():
    """A boost-only custom EQ curve must raise gain at its centre frequencies
    and stay near unity elsewhere.

    Regression test: the previous implementation built each band with
    scipy.signal.iirpeak, which is a *bandpass* resonator (unity at centre,
    rolling off to ~zero elsewhere) rather than a peaking-EQ biquad. Cascading
    five of them multiplied those roll-offs together, so the shipped 5-band
    "custom" curve applied roughly 29 dB of broadband attenuation instead of
    the intended boost.
    """
    import numpy as np
    from scipy.signal import sosfreqz
    from src.audio.output import _build_custom_sos

    bands = [(80.0, 10.0, 1.0), (250.0, 7.0, 1.0), (1000.0, 4.0, 1.0),
             (4000.0, 5.0, 1.0), (12000.0, 7.0, 1.0)]
    sos = _build_custom_sos(bands, 44100)
    assert sos is not None

    w, h = sosfreqz(sos, worN=8192, fs=44100)
    mag_db = 20 * np.log10(np.abs(h) + 1e-12)

    for centre, gain_db, _q in bands:
        idx = int(np.argmin(np.abs(w - centre)))
        assert mag_db[idx] > gain_db - 3.0, (
            f"{centre} Hz should be boosted ~{gain_db} dB, got {mag_db[idx]:.2f} dB"
        )

    # And nowhere should this boost-only curve attenuate meaningfully.
    assert mag_db.min() > -3.0, f"unexpected attenuation: {mag_db.min():.2f} dB"


def test_soft_limit_preserves_quiet_signal_and_caps_peaks():
    """_soft_limit must leave sub-threshold audio untouched and keep peaks
    under the ceiling without hard-clipping."""
    import numpy as np
    from src.audio.output import _soft_limit

    quiet = (np.sin(np.linspace(0, 50, 2000)) * 0.2).astype(np.float32)
    assert np.allclose(_soft_limit(quiet), quiet)

    hot = (np.sin(np.linspace(0, 50, 2000)) * 2.5).astype(np.float32)
    out = _soft_limit(hot)
    assert float(np.max(np.abs(out))) <= 0.97 + 1e-6
    # Must retain more energy than naive hard clipping would discard.
    assert float(np.sqrt(np.mean(out ** 2))) > 0.4
    assert np.all(np.isfinite(out))
