"""
Media client proxies — drop-in stand-ins for `MusicService`/`PodcastService`
that live in `desktop-assistant-core`'s process after those two services were
extracted into their own `media` process (see
docs/architecture/PROCESS_ISOLATION_PROPOSAL.md, Phase 1).

`WebService` was written against `MusicService`/`PodcastService`'s direct
Python object API (properties + methods). Rather than rewrite its ~27 call
sites, `MusicServiceProxy`/`PodcastServiceProxy` replicate that exact public
API but forward every call over `IPCClient` to the `media` process's
`IPCBridge` REP endpoint. `WebService` (and anything else holding a
reference) does not need to know the real service moved to another process.

Fire-and-forget commands (play/stop/next/pause/thumbs-up/…) are unaffected
by this split — they're published as bus events by skills/CLI/WebService
and reach the media process automatically via the existing upstream/
downstream `IPCBridge` forwarding (the same mechanism that already carries
thermal.* events into core). Only calls that need a synchronous return value
(reading state, searching, subscribing, …) go through these proxies.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.ipc_client import IPCClient

log = logging.getLogger(__name__)


def _unwrap(reply: dict, key: str, default):
    """Pull *key* out of an IPCClient reply, logging + falling back on failure.

    IPCClient.call() never raises — it returns {"ok": False, "error": ...}
    for timeouts/connection issues. Callers of the read-only properties below
    don't expect exceptions (the in-process originals never raised for a
    simple property read), so we degrade to a safe default instead, matching
    the pre-split "if not self._music_svc: <default>" fallback behavior that
    used to run when the service was absent/misconfigured.
    """
    if not reply.get("ok"):
        log.warning("media RPC failed (key=%s): %s", key, reply.get("error"))
        return default
    return reply.get(key, default)


def _raise_on_error(reply: dict):
    """Raise RuntimeError(msg) if *reply* indicates failure.

    Mirrors the original services raising on bad input (e.g. `search()` on
    an empty query) — callers in web_service.py already catch `Exception`
    generically and turn it into an HTTPException, so any exception type
    with a useful `str()` works.
    """
    if not reply.get("ok"):
        raise RuntimeError(reply.get("error", "media service request failed"))
    return reply


class MusicServiceProxy:
    """Proxies `MusicService`'s public API to the `media` process."""

    name = "music"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def _call(self, cmd: str, **payload) -> dict:
        return self._client.call({"cmd": cmd, **payload})

    @property
    def state(self) -> str:
        return _unwrap(self._call("music.get_state"), "state", "stopped")

    @property
    def current_song(self) -> dict:
        return _unwrap(self._call("music.get_state"), "current_song", {})

    @property
    def stations(self) -> list:
        return _unwrap(self._call("music.get_state"), "stations", [])

    @property
    def is_configured(self) -> bool:
        return _unwrap(self._call("music.get_state"), "configured", False)

    @property
    def volume(self) -> int:
        return _unwrap(self._call("music.get_state"), "volume", -1)

    @property
    def muted(self) -> bool:
        return _unwrap(self._call("music.get_state"), "muted", False)

    @property
    def eq_preset(self) -> str:
        return _unwrap(self._call("music.get_state"), "eq_preset", "flat")

    def set_volume(self, level: int) -> None:
        self._call("music.set_volume", level=level)

    def set_muted(self, muted: bool) -> None:
        self._call("music.set_muted", muted=bool(muted))

    def set_eq_preset(self, preset: str) -> None:
        self._call("music.set_eq_preset", preset=preset)

    def mark_eq_custom(self) -> None:
        self._call("music.mark_eq_custom")


class PodcastServiceProxy:
    """Proxies `PodcastService`'s public API to the `media` process."""

    name = "podcast"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def _call(self, cmd: str, **payload) -> dict:
        return self._client.call({"cmd": cmd, **payload})

    @property
    def subscriptions(self) -> list:
        return _unwrap(self._call("podcast.list"), "subscriptions", [])

    def search(self, query: str, limit: int = 10) -> list:
        reply = _raise_on_error(self._call("podcast.search", query=query, limit=limit))
        return reply.get("results", [])

    def subscribe(self, query_or_url: str) -> dict:
        return _raise_on_error(self._call("podcast.subscribe", query_or_url=query_or_url))

    def unsubscribe(self, podcast_id: str) -> dict:
        return _raise_on_error(self._call("podcast.unsubscribe", podcast_id=podcast_id))

    def episodes(self, podcast_id: str, limit: int = 20) -> list:
        reply = _raise_on_error(
            self._call("podcast.episodes", podcast_id=podcast_id, limit=limit)
        )
        return reply.get("episodes", [])

    def refresh(self, podcast_id: str) -> dict:
        return _raise_on_error(self._call("podcast.refresh", podcast_id=podcast_id))

    def play(self, podcast_id: str, episode_index: int = 0) -> dict:
        return _raise_on_error(
            self._call("podcast.play", podcast_id=podcast_id, episode_index=episode_index)
        )

    def stop(self) -> dict:
        return _raise_on_error(self._call("podcast.stop"))

    def pause(self) -> dict:
        return _raise_on_error(self._call("podcast.pause"))

    def resume(self) -> dict:
        return _raise_on_error(self._call("podcast.resume"))

    def seek(self, position_sec: float) -> dict:
        return _raise_on_error(self._call("podcast.seek", position_sec=position_sec))

    def skip(self, delta_sec: float) -> dict:
        return _raise_on_error(self._call("podcast.skip", delta_sec=delta_sec))

    def status(self) -> dict:
        reply = self._call("podcast.status")
        if not reply.get("ok"):
            log.warning("media RPC failed (podcast.status): %s", reply.get("error"))
            return {"ok": True, "state": "stopped", "subscriptions": 0, "player": None}
        return reply


def build_media_proxies(rep_endpoint: str, timeout_ms: int = 2000) -> tuple:
    """Convenience factory: one `IPCClient` shared by both proxies."""
    client = IPCClient(rep_endpoint, timeout_ms=timeout_ms)
    return MusicServiceProxy(client), PodcastServiceProxy(client)
