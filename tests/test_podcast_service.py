"""Stub tests for PodcastService."""

from unittest.mock import MagicMock
import pytest

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


def test_lifecycle_stop_is_not_shadowed_by_stop_playback(tmp_path, monkeypatch):
    """Regression test: PodcastService previously defined a business method
    named `stop()` (stop playback) that had the exact same name as the
    inherited `Service.stop()` lifecycle method the runner calls on
    shutdown. That silently shadowed it — `on_stop()`/thread-join/the
    `service.stopped` bus event never ran, and calling `svc.stop()` from
    the runner just stopped podcast playback instead of the service.

    The business method is now `stop_playback()`; `stop()` must remain the
    unmodified `Service` lifecycle method."""
    monkeypatch.setattr(podcast_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(podcast_module, "_PODCASTS_STATE_FILE", tmp_path / "podcasts.json")

    svc = PodcastService(bus=MessageBus())
    events = []
    svc.bus.subscribe("service.stopped", lambda _t, payload: events.append(payload))

    svc.start()
    assert svc.is_running() is True

    svc.stop()

    assert svc.is_running() is False
    assert events == [{"name": "podcast"}]


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


def test_duration_to_seconds_parses_common_formats():
    assert PodcastService._duration_to_seconds("95") == 95.0
    assert PodcastService._duration_to_seconds("01:35") == 95.0
    assert PodcastService._duration_to_seconds("1:01:35") == 3695.0
    assert PodcastService._duration_to_seconds("") is None


def test_seek_and_skip_update_position(monkeypatch):
    svc = PodcastService(bus=MessageBus())

    class _Proc:
        def __init__(self, pid=101):
            self.pid = pid

        def poll(self):
            return None

        def terminate(self):
            return None

        def wait(self, timeout=None):
            return None

        def kill(self):
            return None

    old_proc = _Proc(pid=100)
    new_proc = _Proc(pid=101)

    monkeypatch.setattr(svc, "_spawn_player", MagicMock(return_value=("mpv", new_proc)))
    monkeypatch.setattr(svc, "_terminate_proc", MagicMock())

    svc._player_proc = old_proc
    svc._player_name = "mpv"
    svc._playback.update({
        "state": "playing",
        "paused": False,
        "audio_url": "https://example.com/ep1.mp3",
        "duration_sec": 300.0,
        "seek_sec": 10.0,
        "resumed_at_mono": 100.0,
    })

    monkeypatch.setattr(podcast_module.time, "monotonic", lambda: 110.0)
    out = svc.seek(40.0)
    assert out["position_sec"] == pytest.approx(40.0, abs=0.75)

    monkeypatch.setattr(podcast_module.time, "monotonic", lambda: 111.0)
    out2 = svc.skip(30.0)
    assert out2["position_sec"] == pytest.approx(71.0, abs=0.75)


def test_stop_playback_terminates_process_and_resets_state():
    """`stop_playback()` (the business "stop podcast playback" action,
    distinct from the `Service.stop()` lifecycle method) terminates the
    active player process and resets playback state to stopped."""
    svc = PodcastService(bus=MessageBus())

    class _Proc:
        def __init__(self):
            self.terminated = False

        def terminate(self):
            self.terminated = True

        def wait(self, timeout=None):
            return None

        def kill(self):
            pass

    proc = _Proc()
    svc._player_proc = proc
    svc._player_name = "mpv"
    svc._playback.update({"state": "playing", "paused": False})

    out = svc.stop_playback()

    assert proc.terminated is True
    assert out["state"] == "stopped"
    assert svc._player_proc is None
