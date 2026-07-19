"""
Media entry point — Pandora (pianobar) + podcast (Apple Podcasts/RSS)
playback, isolated from `desktop-assistant-core`.

This is Phase 1 of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md: the
lowest-risk service group to split out first, since neither `MusicService`
nor `PodcastService` holds a direct reference to any other service (unlike
`WebService`, which is the highest-effort, highest-value split and stays in
core for now).

Wiring
------
- Owns its own `MessageBus` + `IPCBridge`, exactly like `thermal_main.py`.
- Subscribes *upstream* to core's PUB endpoint so bus-published commands
  from skills/CLI/WebService (`music.play`, `music.set_volume`,
  `podcast.*`, …) — which are published onto *core's* bus — are forwarded
  onto this process's local bus, where `MusicService`/`PodcastService`'s
  normal `bus.subscribe(...)` handlers pick them up unchanged.
- `core_main.py`'s own `IPCBridge` adds this process's PUB endpoint as one
  more upstream (alongside thermal's), so state-change events published
  here (`music.state_changed`, `music.song_changed`, `av.say`, …) still
  reach core's bus/CLI/dashboard exactly as they did when music ran
  in-process.
- Registers RPC handlers for every synchronous call `WebService` needs
  (via `src/core/media_client.py`'s proxies) — reads (`music.get_state`,
  `podcast.list`, `podcast.status`, …) and actions with return values
  (`podcast.search`, `podcast.subscribe`, `podcast.play`, …).

Run:
    python3 -m src.assistant.media_main

Or via systemd: services/systemd/desktop-assistant-media.service
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from src.core.process_node import ProcessNode
from src.services.music_service import MusicService
from src.services.podcast_service import PodcastService

# Core's IPCBridge PUBs here; we SUBscribe to it so skill/CLI/WebService
# commands (published onto core's bus) reach MusicService/PodcastService's
# bus.subscribe() handlers running in this process.
_CORE_PUB = "ipc:///tmp/desktop-assistant.pub"

# This process's own endpoints — core's IPCBridge adds MEDIA_PUB as one of
# its upstream_endpoints, symmetric to how it already does for thermal.
MEDIA_PUB = "ipc:///tmp/desktop-assistant-media.pub"
MEDIA_REP = "ipc:///tmp/desktop-assistant-media.rep"


def _load_config() -> dict:
    import yaml

    cfg_path = Path(__file__).parents[2] / "config" / "assistant.yaml"
    return yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}


def build_node(
    cfg: dict,
    pub_endpoint: str = MEDIA_PUB,
    rep_endpoint: str = MEDIA_REP,
    upstream_endpoints: Optional[list] = None,
) -> ProcessNode:
    """Build (but don't run) a fully-wired media `ProcessNode`.

    Factored out of `main()` so integration tests can construct a real node
    bound to unique test-only `ipc://` endpoints instead of the production
    ones, without duplicating the RPC registration logic.
    """
    node = ProcessNode(
        name="media",
        pub_endpoint=pub_endpoint,
        rep_endpoint=rep_endpoint,
        upstream_endpoints=[_CORE_PUB] if upstream_endpoints is None else upstream_endpoints,
    )

    music_cfg = cfg.get("music", {})
    music = MusicService(
        bus=node.bus,
        enabled=bool(music_cfg.get("enabled", True)),
        announce_song_changes=bool(music_cfg.get("announce_song_changes", False)),
    )

    podcast_cfg = cfg.get("podcast", {})
    podcast = PodcastService(
        bus=node.bus,
        enabled=bool(podcast_cfg.get("enabled", True)),
        auto_refresh_on_start=bool(podcast_cfg.get("auto_refresh_on_start", False)),
    )

    node.add_services(music, podcast)

    # ── Music RPCs ───────────────────────────────────────────────────
    def _rpc_music_get_state(_msg):
        song = music.current_song
        return {
            "ok": True,
            "state": music.state,
            "current_song": song,
            "stations": music.stations,
            "configured": music.is_configured,
            "volume": music.volume,
            "muted": music.muted,
            "eq_preset": music.eq_preset,
        }

    def _rpc_music_set_volume(msg):
        level = msg.get("level")
        if level is None:
            return {"ok": False, "error": "missing level"}
        music.set_volume(int(level))
        return {"ok": True, "volume": music.volume}

    def _rpc_music_set_muted(msg):
        music.set_muted(bool(msg.get("muted", False)))
        return {"ok": True, "muted": music.muted}

    def _rpc_music_set_eq_preset(msg):
        preset = msg.get("preset")
        if not preset:
            return {"ok": False, "error": "missing preset"}
        music.set_eq_preset(str(preset))
        return {"ok": True, "eq_preset": music.eq_preset}

    def _rpc_music_mark_eq_custom(_msg):
        music.mark_eq_custom()
        return {"ok": True, "eq_preset": music.eq_preset}

    node.register_rpc("music.get_state", _rpc_music_get_state)
    node.register_rpc("music.set_volume", _rpc_music_set_volume)
    node.register_rpc("music.set_muted", _rpc_music_set_muted)
    node.register_rpc("music.set_eq_preset", _rpc_music_set_eq_preset)
    node.register_rpc("music.mark_eq_custom", _rpc_music_mark_eq_custom)

    # ── Podcast RPCs ─────────────────────────────────────────────────
    def _rpc_podcast_list(_msg):
        return {"ok": True, "subscriptions": podcast.subscriptions}

    def _rpc_podcast_search(msg):
        query = msg.get("query", "")
        limit = int(msg.get("limit", 10))
        try:
            results = podcast.search(query, limit=limit)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "results": results}

    def _rpc_podcast_subscribe(msg):
        try:
            return {"ok": True, **podcast.subscribe(msg.get("query_or_url", ""))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_unsubscribe(msg):
        try:
            return {"ok": True, **podcast.unsubscribe(msg.get("podcast_id", ""))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_episodes(msg):
        podcast_id = msg.get("podcast_id", "")
        limit = int(msg.get("limit", 20))
        try:
            episodes = podcast.episodes(podcast_id, limit=limit)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "episodes": episodes}

    def _rpc_podcast_refresh(msg):
        try:
            return {"ok": True, **podcast.refresh(msg.get("podcast_id", ""))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_play(msg):
        podcast_id = msg.get("podcast_id", "")
        episode_index = int(msg.get("episode_index", 0))
        try:
            return {"ok": True, **podcast.play(podcast_id, episode_index)}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_stop(_msg):
        return {"ok": True, **podcast.stop_playback()}

    def _rpc_podcast_pause(_msg):
        try:
            return {"ok": True, **podcast.pause()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_resume(_msg):
        try:
            return {"ok": True, **podcast.resume()}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_seek(msg):
        try:
            return {"ok": True, **podcast.seek(float(msg.get("position_sec", 0.0)))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_skip(msg):
        try:
            return {"ok": True, **podcast.skip(float(msg.get("delta_sec", 0.0)))}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def _rpc_podcast_status(_msg):
        return {"ok": True, **podcast.status()}

    node.register_rpc("podcast.list", _rpc_podcast_list)
    node.register_rpc("podcast.search", _rpc_podcast_search)
    node.register_rpc("podcast.subscribe", _rpc_podcast_subscribe)
    node.register_rpc("podcast.unsubscribe", _rpc_podcast_unsubscribe)
    node.register_rpc("podcast.episodes", _rpc_podcast_episodes)
    node.register_rpc("podcast.refresh", _rpc_podcast_refresh)
    node.register_rpc("podcast.play", _rpc_podcast_play)
    node.register_rpc("podcast.stop", _rpc_podcast_stop)
    node.register_rpc("podcast.pause", _rpc_podcast_pause)
    node.register_rpc("podcast.resume", _rpc_podcast_resume)
    node.register_rpc("podcast.seek", _rpc_podcast_seek)
    node.register_rpc("podcast.skip", _rpc_podcast_skip)
    node.register_rpc("podcast.status", _rpc_podcast_status)

    return node


def main() -> int:
    node = build_node(_load_config())
    return node.run()


if __name__ == "__main__":
    sys.exit(main())
