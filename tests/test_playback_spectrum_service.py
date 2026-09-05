"""Tests for PlaybackSpectrumService band analysis and playback gating."""
import numpy as np
import pytest
from unittest.mock import MagicMock

from src.services.playback_spectrum_service import (
    PlaybackSpectrumConfig,
    PlaybackSpectrumService,
)


def _svc(**kw):
    cfg = PlaybackSpectrumConfig(**kw)
    return PlaybackSpectrumService(bus=MagicMock(), config=cfg)


def _tone_bytes(hz, sr=44100, n=3675, amp=0.6, channels=2):
    t = np.arange(n) / sr
    mono = (amp * np.sin(2 * np.pi * hz * t) * 32767).astype(np.int16)
    if channels == 1:
        return mono.tobytes()
    return np.repeat(mono, channels).tobytes()


def test_service_import_and_construct():
    assert _svc() is not None


def test_band_count_matches_config():
    svc = _svc(bands=12)
    bins = None
    for _ in range(8):
        bins = svc._compute_bands(_tone_bytes(100), 2)
    assert bins is not None
    assert len(bins) == 12


def test_low_tone_lights_low_bands_only():
    """A 100 Hz tone must appear at the bottom of the spectrum, not the top."""
    svc = _svc(bands=12)
    for _ in range(8):
        bins = svc._compute_bands(_tone_bytes(100), 2)
    assert max(bins[:3]) > 0.3
    assert max(bins[-4:]) < 0.05


def test_high_tone_lights_high_bands_only():
    svc = _svc(bands=12)
    for _ in range(8):
        bins = svc._compute_bands(_tone_bytes(9000), 2)
    assert max(bins[-5:]) > 0.2
    assert max(bins[:3]) < 0.05


def test_silence_produces_zero_bands():
    svc = _svc(bands=12)
    quiet = np.zeros(3675 * 2, dtype=np.int16).tobytes()
    for _ in range(12):
        bins = svc._compute_bands(quiet, 2)
    assert all(v <= 0.01 for v in bins)


def test_values_are_normalized_0_to_1():
    svc = _svc(bands=12)
    for _ in range(8):
        bins = svc._compute_bands(_tone_bytes(1000, amp=0.99), 2)
    assert all(0.0 <= v <= 1.0 for v in bins)


def test_mono_capture_supported():
    svc = _svc(bands=8, channels=1)
    bins = svc._compute_bands(_tone_bytes(100, channels=1), 1)
    assert bins is not None and len(bins) == 8


def test_short_buffer_returns_none():
    svc = _svc()
    assert svc._compute_bands(b"\x00\x00", 2) is None


def test_decay_is_slower_than_attack():
    """Bars should snap up on transients but fall back gradually."""
    svc = _svc(bands=8)
    loud = _tone_bytes(100, amp=0.9)
    quiet = np.zeros(3675 * 2, dtype=np.int16).tobytes()
    for _ in range(10):
        peak = svc._compute_bands(loud, 2)
    after_one_quiet = svc._compute_bands(quiet, 2)
    top = int(np.argmax(peak))
    # One silent frame must not collapse the bar to zero.
    assert after_one_quiet[top] > 0.0
    assert after_one_quiet[top] < peak[top]


# ── Playback gating ─────────────────────────────────────────────────────
#
# The capture subprocess must only run while something is actually playing;
# otherwise an idle VERA holds a permanent PipeWire monitor stream.

def test_music_playing_activates_capture():
    svc = _svc()
    svc._on_music_state("music.state_changed", {"state": "playing"})
    assert svc._active.is_set()


def test_music_stopped_deactivates_capture():
    svc = _svc()
    svc._on_music_state("music.state_changed", {"state": "playing"})
    svc._on_music_state("music.state_changed", {"state": "stopped"})
    assert not svc._active.is_set()


def test_music_paused_deactivates_capture():
    svc = _svc()
    svc._on_music_state("music.state_changed", {"state": "playing"})
    svc._on_music_state("music.state_changed", {"state": "paused"})
    assert not svc._active.is_set()


def test_podcast_playing_flag_activates_capture():
    svc = _svc()
    svc._on_podcast_state("podcast.playback", {"playing": True})
    assert svc._active.is_set()
    svc._on_podcast_state("podcast.playback", {"playing": False})
    assert not svc._active.is_set()


def test_podcast_state_string_activates_capture():
    svc = _svc()
    svc._on_podcast_state("podcast.playback", {"state": "playing"})
    assert svc._active.is_set()


# ── Monitor-port linking ────────────────────────────────────────────────
#
# `pw-record --target <sink>` does NOT reliably attach to a sink monitor on
# this system: the session manager routes the capture to the default *source*
# instead, silently yielding microphone audio instead of playback. The stream
# must be created unconnected and linked to the sink's monitor_* ports
# explicitly. These tests lock in that behavior.

_HW = "alsa_output.usb-Seeed_reSpeaker-00.analog-stereo"


def test_resolve_sink_prefers_configured_name():
    svc = _svc(sink_name="explicit.sink")
    assert svc._resolve_sink_name() == "explicit.sink"


def test_resolve_sink_autodetects_alsa_monitor(monkeypatch):
    import src.services.playback_spectrum_service as mod

    listing = (
        "some_source:capture_FL\n"
        f"{_HW}:monitor_FL\n"
        f"{_HW}:monitor_FR\n"
    )
    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: MagicMock(stdout=listing, returncode=0),
    )
    assert _svc()._resolve_sink_name() == _HW


def test_spawn_creates_unconnected_stream_and_links_monitor(monkeypatch):
    """The capture must use --target 0, never --target <sink>."""
    import src.services.playback_spectrum_service as mod

    calls = {}
    monkeypatch.setattr(mod, "_PW_RECORD", "/usr/bin/pw-record")
    monkeypatch.setattr(
        mod.subprocess, "Popen",
        lambda cmd, **k: calls.setdefault("cmd", cmd) or MagicMock(),
    )
    svc = _svc(sink_name=_HW)
    monkeypatch.setattr(svc, "_link_monitor", lambda s, n: True)

    assert svc._spawn() is not None
    cmd = calls["cmd"]
    assert "--target" in cmd
    assert cmd[cmd.index("--target") + 1] == "0"
    assert any("stream.capture.sink=true" in str(a) for a in cmd)


def test_spawn_aborts_when_monitor_link_fails(monkeypatch):
    """A stream that can't be linked would capture mic audio — abort instead."""
    import src.services.playback_spectrum_service as mod

    proc = MagicMock()
    monkeypatch.setattr(mod, "_PW_RECORD", "/usr/bin/pw-record")
    monkeypatch.setattr(mod.subprocess, "Popen", lambda cmd, **k: proc)
    svc = _svc(sink_name=_HW)
    monkeypatch.setattr(svc, "_link_monitor", lambda s, n: False)

    assert svc._spawn() is None
    proc.terminate.assert_called_once()


def test_spawn_returns_none_without_sink(monkeypatch):
    svc = _svc()
    monkeypatch.setattr(svc, "_resolve_sink_name", lambda: None)
    assert svc._spawn() is None


def test_link_monitor_links_both_channels(monkeypatch):
    import src.services.playback_spectrum_service as mod

    linked = []

    def fake_run(cmd, **kw):
        linked.append(cmd)
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    assert _svc()._link_monitor(_HW, "veraeqviz1") is True
    joined = [" ".join(c) for c in linked]
    assert any("monitor_FL" in j and "input_FL" in j for j in joined)
    assert any("monitor_FR" in j and "input_FR" in j for j in joined)


def test_link_monitor_treats_existing_link_as_success(monkeypatch):
    import src.services.playback_spectrum_service as mod

    monkeypatch.setattr(
        mod.subprocess, "run",
        lambda *a, **k: MagicMock(returncode=1, stderr="link already exists"),
    )
    assert _svc()._link_monitor(_HW, "veraeqviz1") is True
