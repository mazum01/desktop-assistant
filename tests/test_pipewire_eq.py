"""Tests for src/audio/pipewire_eq.py — config generation and preset coverage."""

import pytest
import src.audio.pipewire_eq as _pweq
from src.audio.pipewire_eq import _build_config, PRESET_BANDS, apply_preset, apply_custom_bands


def test_all_presets_defined():
    from src.services.music_service import MusicService
    # Every named preset in MusicService must have a PipeWire band definition,
    # except "custom" which uses user-supplied bands.
    for p in MusicService.EQ_PRESETS:
        if p != "custom":
            assert p in PRESET_BANDS, f"Missing PipeWire preset: {p!r}"


def test_build_config_structure():
    bands = PRESET_BANDS["bass_boost"]
    cfg = _build_config(bands)
    assert "DA Equalizer" in cfg
    assert "effect_input.da_eq" in cfg
    assert "effect_output.da_eq" in cfg
    assert "bq_lowshelf" in cfg
    assert f"eq_band_{len(bands)}" in cfg


def test_build_config_links():
    bands = PRESET_BANDS["flat"]
    cfg = _build_config(bands)
    # Should have N-1 links for N bands
    link_count = cfg.count("output =")
    assert link_count == len(bands) - 1


def test_build_config_gains():
    bands = PRESET_BANDS["bass_boost"]
    cfg = _build_config(bands)
    # First band (lowshelf) should have +6 dB
    assert '"Gain" = 6.00' in cfg


def test_apply_preset_unknown_returns_false(monkeypatch):
    result = apply_preset("nonexistent_preset")
    assert result is False


def test_apply_custom_empty_bands(monkeypatch):
    """Empty bands should delegate to 'flat' preset."""
    calls = []
    monkeypatch.setattr("src.audio.pipewire_eq._apply_bands",
                        lambda bands, label="": calls.append(bands) or True)
    apply_custom_bands([])
    # apply_preset("flat") was called, which calls _apply_bands with flat bands
    assert len(calls) == 1
    assert len(calls[0]) == len(PRESET_BANDS["flat"])


def test_custom_band_conversion(monkeypatch):
    """Custom bands dict format should convert to (hz, gain_db, q, type) tuples."""
    captured = []
    monkeypatch.setattr("src.audio.pipewire_eq._apply_bands",
                        lambda bands, label="": captured.append(bands) or True)
    apply_custom_bands([{"hz": 1000, "gain_db": 3.5, "q": 1.2}])
    assert len(captured) == 1
    hz, gain, q, ftype = captured[0][0]
    assert hz == 1000.0
    assert gain == 3.5
    assert q == 1.2
    assert ftype == "peaking"


# ── Live-update path tests ────────────────────────────────────────────────────

def test_live_update_used_when_sink_active(monkeypatch, tmp_path):
    """When the EQ sink is already running, _apply_bands should try the live
    update path first and skip the filter-chain restart entirely."""
    restart_calls = []
    live_calls = []

    monkeypatch.setattr(_pweq, "_active", True)
    monkeypatch.setattr(_pweq, "_get_eq_sink_id", lambda: "42")
    monkeypatch.setattr(_pweq, "_try_live_update", lambda bands: live_calls.append(bands) or True)
    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: restart_calls.append(1) or False)
    # Redirect config write to tmp_path
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")

    result = _pweq._apply_bands(PRESET_BANDS["bass_boost"], label="test")

    assert result is True
    assert len(live_calls) == 1, "live update should have been attempted"
    assert len(restart_calls) == 0, "filter-chain restart must NOT be called on live update"


def test_live_update_fallback_to_restart(monkeypatch, tmp_path):
    """When live update fails, _apply_bands must fall back to the restart path."""
    restart_called = []
    sink_poll_count = [0]

    monkeypatch.setattr(_pweq, "_active", True)
    monkeypatch.setattr(_pweq, "_get_eq_sink_id", lambda: "42" if sink_poll_count[0] > 0 else "42")
    monkeypatch.setattr(_pweq, "_try_live_update", lambda bands: False)
    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: restart_called.append(1) or True)
    monkeypatch.setattr(_pweq, "_set_default_sink", lambda sid: True)
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")
    # Make sink appear immediately after restart (skip the polling sleep)
    import src.audio.pipewire_eq as _mod
    monkeypatch.setattr(_mod.time, "sleep", lambda _: None)

    result = _pweq._apply_bands(PRESET_BANDS["flat"], label="test")

    assert len(restart_called) == 1, "filter-chain restart must be called when live update fails"
    assert result is True


def test_try_live_update_insufficient_nodes(monkeypatch):
    """_try_live_update returns False when fewer nodes are found than bands."""
    monkeypatch.setattr(_pweq, "_get_eq_band_node_ids", lambda: {1: "10", 2: "11"})
    bands = PRESET_BANDS["flat"]  # 5 bands
    assert _pweq._try_live_update(bands) is False


def test_try_live_update_success(monkeypatch):
    """_try_live_update returns True when all band updates succeed."""
    bands = PRESET_BANDS["bass_boost"]  # 5 bands
    node_ids = {i: str(i + 100) for i in range(1, len(bands) + 1)}
    monkeypatch.setattr(_pweq, "_get_eq_band_node_ids", lambda: node_ids)
    monkeypatch.setattr(_pweq, "_update_band_props",
                        lambda nid, hz, gain, q: True)
    assert _pweq._try_live_update(bands) is True


def test_try_live_update_partial_failure(monkeypatch):
    """_try_live_update returns False if any band update fails."""
    bands = PRESET_BANDS["bass_boost"]
    node_ids = {i: str(i + 100) for i in range(1, len(bands) + 1)}
    monkeypatch.setattr(_pweq, "_get_eq_band_node_ids", lambda: node_ids)
    call_count = [0]

    def _fail_on_third(nid, hz, gain, q):
        call_count[0] += 1
        return call_count[0] != 3  # fail on 3rd call

    monkeypatch.setattr(_pweq, "_update_band_props", _fail_on_third)
    assert _pweq._try_live_update(bands) is False

