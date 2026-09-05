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
    cfg = _build_config(bands, makeup_db=0.0)
    # Should have N-1 links for N bands
    link_count = cfg.count("output =")
    assert link_count == len(bands) - 1


def test_build_config_makeup_stage_chained_after_eq():
    """Makeup gain + clamp must terminate the chain, not float unconnected.

    Music (pianobar) streams straight into PipeWire and never passes through
    AudioOutput's software loudness_boost, so this in-graph makeup stage is
    the only thing that brings music up to speech level.
    """
    bands = PRESET_BANDS["flat"]
    cfg = _build_config(bands, makeup_db=6.0)
    last = len(bands)
    assert "label = linear" in cfg
    assert "label = clamp" in cfg
    # 6 dB -> ~1.9953 linear
    assert '"Gain" = 1.9953' in cfg
    assert f'{{ output = "eq_band_{last}:Out"  input = "makeup:In" }}' in cfg
    assert '{ output = "makeup:Out"  input = "ceiling:In" }' in cfg
    # N-1 band links + 2 makeup links
    assert cfg.count("output =") == len(bands) + 1


def test_build_config_no_makeup_when_zero():
    cfg = _build_config(PRESET_BANDS["flat"], makeup_db=0.0)
    assert "label = linear" not in cfg
    assert "label = clamp" not in cfg


def test_build_config_defaults_to_module_makeup_gain():
    import src.audio.pipewire_eq as pw

    cfg = _build_config(PRESET_BANDS["flat"])
    expected = 10 ** (pw.MAKEUP_GAIN_DB / 20.0)
    assert f'"Gain" = {expected:.4f}' in cfg


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


# ── _apply_bands path tests ───────────────────────────────────────────────────

def test_apply_bands_restarts_filter_chain(monkeypatch, tmp_path):
    """_apply_bands must always restart filter-chain to apply new EQ config."""
    restart_calls = []
    migrate_calls = []

    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: restart_calls.append(1) or True)
    monkeypatch.setattr(_pweq, "_get_eq_sink_id", lambda: "42")
    monkeypatch.setattr(_pweq, "_set_default_sink", lambda sid: True)
    monkeypatch.setattr(_pweq, "_migrate_streams", lambda sid: migrate_calls.append(sid))
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")
    monkeypatch.setattr(_pweq.time, "sleep", lambda _: None)

    result = _pweq._apply_bands(PRESET_BANDS["bass_boost"], label="test")

    assert result is True
    assert len(restart_calls) == 1, "filter-chain restart must always be called"


def test_apply_bands_migrates_streams_on_success(monkeypatch, tmp_path):
    """_migrate_streams must be called with the sink_id after a successful apply."""
    migrate_calls = []

    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: True)
    monkeypatch.setattr(_pweq, "_get_eq_sink_id", lambda: "42")
    monkeypatch.setattr(_pweq, "_set_default_sink", lambda sid: True)
    monkeypatch.setattr(_pweq, "_migrate_streams", lambda sid: migrate_calls.append(sid))
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")
    monkeypatch.setattr(_pweq.time, "sleep", lambda _: None)

    _pweq._apply_bands(PRESET_BANDS["flat"], label="test")

    assert migrate_calls == ["42"], "_migrate_streams must be called with the correct sink_id"


def test_apply_bands_no_migration_on_failure(monkeypatch, tmp_path):
    """_migrate_streams must NOT be called when apply fails."""
    migrate_calls = []

    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: True)
    # Sink never appears — apply fails
    monkeypatch.setattr(_pweq, "_get_eq_sink_id", lambda: None)
    monkeypatch.setattr(_pweq, "_migrate_streams", lambda sid: migrate_calls.append(sid))
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")
    monkeypatch.setattr(_pweq.time, "sleep", lambda _: None)

    result = _pweq._apply_bands(PRESET_BANDS["flat"], label="test")

    assert result is False
    assert len(migrate_calls) == 0, "_migrate_streams must not be called on failure"


def test_apply_bands_restart_failure_returns_false(monkeypatch, tmp_path):
    """When filter-chain restart fails, _apply_bands returns False."""
    monkeypatch.setattr(_pweq, "_restart_filter_chain", lambda: False)
    monkeypatch.setattr(_pweq, "_CONF_DIR", tmp_path)
    monkeypatch.setattr(_pweq, "_CONF_FILE", tmp_path / "da-eq.conf")

    result = _pweq._apply_bands(PRESET_BANDS["flat"], label="test")
    assert result is False




def test_ensure_default_retries_until_sink_appears(monkeypatch, tmp_path):
    """ensure_default() must retry (like _apply_bands) instead of giving up
    on a single lookup — a cold boot can have filter-chain still spinning up
    the effect_input.da_eq node by the time AVService.on_start() runs, even
    though the same lookup succeeds instantly on a warm service restart."""
    conf_file = tmp_path / "da-eq.conf"
    conf_file.write_text("# config")
    monkeypatch.setattr(_pweq, "_CONF_FILE", conf_file)
    monkeypatch.setattr(_pweq.time, "sleep", lambda _: None)

    calls = {"n": 0}

    def _flaky_lookup():
        calls["n"] += 1
        return "146" if calls["n"] >= 3 else None

    monkeypatch.setattr(_pweq, "_get_eq_sink_id", _flaky_lookup)
    set_default_calls = []
    monkeypatch.setattr(_pweq, "_set_default_sink",
                         lambda sid: set_default_calls.append(sid) or True)
    monkeypatch.setattr(_pweq, "_restore_volumes", lambda sid: None)

    _pweq.ensure_default()

    assert calls["n"] == 3
    assert set_default_calls == ["146"]
    assert _pweq.is_active() is True


def test_ensure_default_gives_up_after_max_retries(monkeypatch, tmp_path):
    """If the sink never appears, ensure_default() must not hang forever or
    silently mark itself active."""
    conf_file = tmp_path / "da-eq.conf"
    conf_file.write_text("# config")
    monkeypatch.setattr(_pweq, "_CONF_FILE", conf_file)
    monkeypatch.setattr(_pweq.time, "sleep", lambda _: None)
    monkeypatch.setattr(_pweq, "_active", False)

    calls = {"n": 0}

    def _never_appears():
        calls["n"] += 1
        return None

    monkeypatch.setattr(_pweq, "_get_eq_sink_id", _never_appears)
    monkeypatch.setattr(_pweq, "_set_default_sink",
                         lambda sid: pytest.fail("should not be called"))

    _pweq.ensure_default()

    assert calls["n"] == 50
    assert _pweq.is_active() is False
