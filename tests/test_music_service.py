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
