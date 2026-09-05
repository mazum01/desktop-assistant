"""Stub tests for MusicService."""
import pytest
from unittest.mock import MagicMock, patch


def test_music_service_import():
    """MusicService can be imported without error."""
    from src.services.music_service import MusicService  # noqa: F401


def test_music_service_instantiation():
    """MusicService initialises without errors when bus is mocked."""
    from src.services.music_service import MusicService

    bus = MagicMock()
    bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)
    assert svc is not None


def test_music_service_default_state():
    """MusicService starts in 'stopped' state."""
    from src.services.music_service import MusicService

    bus = MagicMock()
    bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)
    assert svc.state == "stopped"


def test_music_service_current_song_empty():
    """current_song returns a dict with metadata fields even when nothing is playing."""
    from src.services.music_service import MusicService

    bus = MagicMock()
    bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)
    song = svc.current_song
    assert isinstance(song, dict)
    # No pianobar metadata yet — title/artist absent, progress fields at zero.
    assert "title" not in song
    assert song.get("elapsed_sec", 0) == 0
    assert song.get("duration_sec", 0) == 0


def test_music_service_stations_empty():
    """stations returns an empty list before pianobar reports them."""
    from src.services.music_service import MusicService

    bus = MagicMock()
    bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)
    assert svc.stations == []


# ── Stale PipeWire sink-ID cache regressions ────────────────────────────────
#
# PipeWire assigns a NEW node ID every time filter-chain.service restarts
# (which happens whenever the user saves a custom EQ profile). The sink ID was
# cached with no invalidation, so every volume write after an EQ save targeted
# a dead node — while still reporting success to the web GUI.

def test_get_sink_id_rediscovers_when_cached_id_is_dead():
    """A cached ID that no longer exists must not be returned."""
    import src.services.music_service as ms

    ms._CACHED_SINK_ID = "126"  # stale ID from before a filter-chain restart
    try:
        with patch.object(ms, "_sink_id_is_live", return_value=False), \
             patch.object(ms, "_get_default_sink", return_value="149"):
            assert ms._get_sink_id() == "149"
    finally:
        ms._CACHED_SINK_ID = None


def test_get_sink_id_returns_cached_id_while_live():
    import src.services.music_service as ms

    ms._CACHED_SINK_ID = "149"
    try:
        with patch.object(ms, "_sink_id_is_live", return_value=True), \
             patch.object(ms, "_get_default_sink") as disc:
            assert ms._get_sink_id() == "149"
            disc.assert_not_called()
    finally:
        ms._CACHED_SINK_ID = None


def test_invalidate_sink_cache_clears_cached_id():
    import src.services.music_service as ms

    ms._CACHED_SINK_ID = "126"
    ms.invalidate_sink_cache()
    assert ms._CACHED_SINK_ID is None


def test_set_volume_retries_on_stale_sink_then_persists():
    """A rejected write must trigger rediscovery and a retry."""
    from src.services.music_service import MusicService
    import src.services.music_service as ms

    bus = MagicMock(); bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)

    results = [MagicMock(returncode=1, stderr="Node '126' not found"),
               MagicMock(returncode=0, stderr="")]
    with patch.object(ms.subprocess, "run", side_effect=results) as run, \
         patch.object(ms, "_get_sink_id", side_effect=["126", "149"]), \
         patch.object(ms, "invalidate_sink_cache") as inval, \
         patch.object(ms.volume_state, "save_volume") as save:
        svc.set_volume(40)

    inval.assert_called_once()
    assert run.call_count == 2
    assert run.call_args_list[1].args[0][2] == "149"
    save.assert_called_once_with(40)


def test_set_volume_does_not_persist_when_write_fails():
    """A failed write must not poison the persisted level.

    Previously set_volume() saved unconditionally, so failures wrote the
    requested level to music_volume.txt anyway — which is how the stored
    volume got stuck at 100.
    """
    from src.services.music_service import MusicService
    import src.services.music_service as ms

    bus = MagicMock(); bus.subscribe = MagicMock()
    svc = MusicService(bus=bus)

    fail = MagicMock(returncode=1, stderr="Node not found")
    with patch.object(ms.subprocess, "run", return_value=fail), \
         patch.object(ms, "_get_sink_id", return_value="126"), \
         patch.object(ms, "invalidate_sink_cache"), \
         patch.object(ms.volume_state, "save_volume") as save:
        svc.set_volume(40)

    save.assert_not_called()
