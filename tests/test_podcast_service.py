"""Stub tests for PodcastService."""

from unittest.mock import MagicMock

import src.services.podcast_service as podcast_module
from src.core.bus import MessageBus
from src.services.podcast_service import PodcastService


def test_podcast_service_import():
    """PodcastService can be imported without error."""
    from src.services.podcast_service import PodcastService as _PodcastService  # noqa: F401


def test_podcast_service_instantiation():
    """PodcastService initialises without errors."""
    svc = PodcastService(bus=MessageBus())
    assert svc is not None
    assert svc.status()["state"] == "stopped"


def test_subscribe_unsubscribe_roundtrip(tmp_path, monkeypatch):
    """Subscribe/unsubscribe updates in-memory subscription list."""
    monkeypatch.setattr(podcast_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(podcast_module, "_PODCASTS_STATE_FILE", tmp_path / "podcasts.json")

    svc = PodcastService(bus=MessageBus())
    svc._fetch_feed = MagicMock(return_value={  # type: ignore[attr-defined]
        "title": "Test Podcast",
        "author": "Tester",
        "artwork_url": "",
        "episodes": [{"id": "ep1", "title": "Episode 1", "audio_url": "https://example.com/ep1.mp3"}],
    })

    out = svc.subscribe("https://example.com/feed.xml")
    assert out["ok"] is True
    pid = out["subscription"]["id"]
    assert len(svc.subscriptions) == 1
    eps = svc.episodes(pid, limit=10)
    assert len(eps) == 1
    assert eps[0]["title"] == "Episode 1"

    deleted = svc.unsubscribe(pid)
    assert deleted["ok"] is True
    assert deleted["deleted"] == 1
    assert svc.subscriptions == []

