"""
Podcast service — Apple Podcasts discovery + RSS playback.

Uses the iTunes Search API to discover podcasts and stores subscriptions in
~/.config/desktop-assistant/podcasts.json. Episode metadata is parsed directly
from each podcast RSS feed.

Playback is delegated to an external audio player subprocess (mpv, ffplay, or
cvlc). Audio still routes through the system default sink so existing mute,
volume, and EQ controls continue to apply.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

_ITUNES_SEARCH_URL = "https://itunes.apple.com/search"
_STATE_DIR = Path.home() / ".config" / "desktop-assistant"
_PODCASTS_STATE_FILE = _STATE_DIR / "podcasts.json"

_ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"


class PodcastService(Service):
    """Podcast subscriptions and playback control."""

    name = "podcast"
    tick_seconds = 1.0

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        enabled: bool = True,
        auto_refresh_on_start: bool = False,
    ) -> None:
        super().__init__(bus=bus)
        self._enabled = bool(enabled)
        self._auto_refresh_on_start = bool(auto_refresh_on_start)
        self._lock = threading.Lock()
        self._subscriptions: list[dict] = []
        self._player_proc: Optional[subprocess.Popen] = None
        self._player_name: Optional[str] = None
        self._playback: dict = {
            "state": "stopped",
            "podcast_id": None,
            "podcast_title": None,
            "episode_id": None,
            "episode_title": None,
            "episode_index": 0,
            "audio_url": None,
            "duration_sec": None,
            "position_sec": 0.0,
            "seek_sec": 0.0,
            "resumed_at_mono": None,
            "started_at": None,
            "paused": False,
        }

    # ── Lifecycle ─────────────────────────────────────────────────────

    def on_start(self) -> None:
        if not self._enabled:
            log.info("PodcastService disabled via config")
            return
        self._load_state()
        if self._auto_refresh_on_start:
            try:
                self.refresh_all()
            except Exception:
                log.exception("PodcastService refresh_all on start failed")
        log.info("PodcastService started (subscriptions=%d)", len(self._subscriptions))

    def run_tick(self) -> None:
        proc: Optional[subprocess.Popen]
        with self._lock:
            proc = self._player_proc
        if proc is None:
            return
        rc = proc.poll()
        if rc is None:
            return
        with self._lock:
            if self._player_proc is proc:
                self._player_proc = None
                self._player_name = None
                self._playback.update({
                    "state": "stopped",
                    "paused": False,
                    "position_sec": 0.0,
                    "seek_sec": 0.0,
                    "resumed_at_mono": None,
                })
        self.bus.publish("podcast.playback", self.status())

    def on_stop(self) -> None:
        self.stop()
        log.info("PodcastService stopped")

    # ── Public API ────────────────────────────────────────────────────

    @property
    def subscriptions(self) -> list[dict]:
        with self._lock:
            return [
                {
                    "id": s.get("id"),
                    "title": s.get("title"),
                    "author": s.get("author"),
                    "feed_url": s.get("feed_url"),
                    "artwork_url": s.get("artwork_url"),
                    "episode_count": len(s.get("episodes") or []),
                    "last_refreshed": s.get("last_refreshed"),
                }
                for s in self._subscriptions
            ]

    def status(self) -> dict:
        with self._lock:
            out = dict(self._playback)
            out["position_sec"] = self._position_sec_locked()
            out["ok"] = True
            out["player"] = self._player_name
            out["subscriptions"] = len(self._subscriptions)
        return out

    def search(self, query: str, limit: int = 10) -> list[dict]:
        query = (query or "").strip()
        if not query:
            return []
        params = {
            "media": "podcast",
            "entity": "podcast",
            "term": query,
            "limit": str(max(1, min(50, int(limit)))),
        }
        url = _ITUNES_SEARCH_URL + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"User-Agent": "desktop-assistant/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        out = []
        for item in data.get("results", []):
            feed = item.get("feedUrl")
            if not feed:
                continue
            out.append({
                "collection_id": item.get("collectionId"),
                "title": item.get("collectionName") or "",
                "author": item.get("artistName") or "",
                "feed_url": feed,
                "artwork_url": item.get("artworkUrl600") or item.get("artworkUrl100") or "",
            })
        return out

    def subscribe(self, query_or_url: str) -> dict:
        src = (query_or_url or "").strip()
        if not src:
            raise ValueError("query_or_url is required")

        search_hit: Optional[dict] = None
        if src.startswith("http://") or src.startswith("https://"):
            feed_url = src
            collection_id = None
        else:
            hits = self.search(src, limit=1)
            if not hits:
                raise ValueError(f"No Apple Podcasts results for query: {src!r}")
            search_hit = hits[0]
            feed_url = search_hit["feed_url"]
            collection_id = search_hit.get("collection_id")

        meta = self._fetch_feed(feed_url)
        pid = str(collection_id) if collection_id else self._stable_id(feed_url)

        sub = {
            "id": pid,
            "title": meta.get("title") or (search_hit.get("title") if search_hit else feed_url),
            "author": meta.get("author") or (search_hit.get("author") if search_hit else ""),
            "feed_url": feed_url,
            "artwork_url": (search_hit.get("artwork_url") if search_hit else "") or meta.get("artwork_url", ""),
            "episodes": meta.get("episodes", []),
            "last_refreshed": time.time(),
        }

        with self._lock:
            idx = next((i for i, s in enumerate(self._subscriptions) if s.get("id") == pid), None)
            if idx is None:
                self._subscriptions.append(sub)
            else:
                self._subscriptions[idx] = sub
            self._save_state_locked()

        self.bus.publish("podcast.subscriptions_updated", {"count": len(self._subscriptions)})
        return {
            "ok": True,
            "subscription": {
                "id": sub["id"],
                "title": sub["title"],
                "author": sub["author"],
                "episode_count": len(sub.get("episodes") or []),
            },
        }

    def unsubscribe(self, podcast_id: str) -> dict:
        with self._lock:
            before = len(self._subscriptions)
            self._subscriptions = [s for s in self._subscriptions if str(s.get("id")) != str(podcast_id)]
            deleted = before - len(self._subscriptions)
            self._save_state_locked()
        self.bus.publish("podcast.subscriptions_updated", {"count": len(self._subscriptions)})
        return {"ok": True, "deleted": deleted}

    def episodes(self, podcast_id: str, limit: int = 20) -> list[dict]:
        with self._lock:
            sub = next((s for s in self._subscriptions if str(s.get("id")) == str(podcast_id)), None)
            if sub is None:
                raise ValueError(f"Unknown podcast id: {podcast_id}")
            eps = list(sub.get("episodes") or [])
        if limit > 0:
            eps = eps[: int(limit)]
        return eps

    def refresh(self, podcast_id: str) -> dict:
        with self._lock:
            sub = next((s for s in self._subscriptions if str(s.get("id")) == str(podcast_id)), None)
            if sub is None:
                raise ValueError(f"Unknown podcast id: {podcast_id}")
            feed_url = sub.get("feed_url")
            if not feed_url:
                raise ValueError("subscription has no feed_url")

        meta = self._fetch_feed(feed_url)
        with self._lock:
            sub = next((s for s in self._subscriptions if str(s.get("id")) == str(podcast_id)), None)
            if sub is None:
                raise ValueError(f"Unknown podcast id: {podcast_id}")
            sub["title"] = meta.get("title") or sub.get("title")
            sub["author"] = meta.get("author") or sub.get("author")
            if meta.get("artwork_url"):
                sub["artwork_url"] = meta.get("artwork_url")
            sub["episodes"] = meta.get("episodes", [])
            sub["last_refreshed"] = time.time()
            self._save_state_locked()
            count = len(sub["episodes"])

        return {"ok": True, "podcast_id": str(podcast_id), "episode_count": count}

    def refresh_all(self) -> dict:
        with self._lock:
            ids = [str(s.get("id")) for s in self._subscriptions]
        ok = 0
        errors = []
        for pid in ids:
            try:
                self.refresh(pid)
                ok += 1
            except Exception as exc:
                errors.append({"id": pid, "error": str(exc)})
        return {"ok": len(errors) == 0, "refreshed": ok, "errors": errors}

    def play(self, podcast_id: str, episode_index: int = 0) -> dict:
        with self._lock:
            sub = next((s for s in self._subscriptions if str(s.get("id")) == str(podcast_id)), None)
            if sub is None:
                raise ValueError(f"Unknown podcast id: {podcast_id}")
            episodes = list(sub.get("episodes") or [])

        if not episodes:
            self.refresh(podcast_id)
            with self._lock:
                sub = next((s for s in self._subscriptions if str(s.get("id")) == str(podcast_id)), None)
                episodes = list((sub or {}).get("episodes") or [])
        if not episodes:
            raise ValueError("No episodes available")

        idx = max(0, min(len(episodes) - 1, int(episode_index)))
        ep = episodes[idx]
        audio_url = ep.get("audio_url")
        if not audio_url:
            raise ValueError("Episode has no audio URL")

        self.stop()
        duration_sec = self._duration_to_seconds(ep.get("duration"))
        player_name, proc = self._spawn_player(audio_url=audio_url, start_offset_sec=0.0)

        with self._lock:
            self._player_proc = proc
            self._player_name = player_name
            self._playback.update({
                "state": "playing",
                "podcast_id": str(podcast_id),
                "podcast_title": sub.get("title"),
                "episode_id": ep.get("id"),
                "episode_title": ep.get("title"),
                "episode_index": idx,
                "audio_url": audio_url,
                "duration_sec": duration_sec,
                "position_sec": 0.0,
                "seek_sec": 0.0,
                "resumed_at_mono": time.monotonic(),
                "started_at": time.time(),
                "paused": False,
            })

        status = self.status()
        self.bus.publish("podcast.playback", status)
        return status

    def stop(self) -> dict:
        with self._lock:
            proc = self._player_proc
            self._player_proc = None
            self._player_name = None
            self._playback.update({
                "state": "stopped",
                "paused": False,
                "position_sec": 0.0,
                "seek_sec": 0.0,
                "resumed_at_mono": None,
            })

        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

        status = self.status()
        self.bus.publish("podcast.playback", status)
        return status

    def pause(self) -> dict:
        with self._lock:
            proc = self._player_proc
            if proc is None or proc.poll() is not None:
                raise ValueError("No active podcast playback")
            if self._playback.get("paused"):
                return self.status()
            self._playback["seek_sec"] = self._position_sec_locked()
            self._playback["resumed_at_mono"] = None
            os.kill(proc.pid, signal.SIGSTOP)
            self._playback["paused"] = True
            self._playback["state"] = "paused"
            self._playback["position_sec"] = self._playback["seek_sec"]
        status = self.status()
        self.bus.publish("podcast.playback", status)
        return status

    def resume(self) -> dict:
        with self._lock:
            proc = self._player_proc
            if proc is None or proc.poll() is not None:
                raise ValueError("No active podcast playback")
            if not self._playback.get("paused"):
                return self.status()
            os.kill(proc.pid, signal.SIGCONT)
            self._playback["paused"] = False
            self._playback["state"] = "playing"
            self._playback["resumed_at_mono"] = time.monotonic()
        status = self.status()
        self.bus.publish("podcast.playback", status)
        return status

    def seek(self, position_sec: float) -> dict:
        with self._lock:
            proc = self._player_proc
            if proc is None or proc.poll() is not None:
                raise ValueError("No active podcast playback")
            audio_url = str(self._playback.get("audio_url") or "")
            if not audio_url:
                raise ValueError("No active podcast audio URL")
            was_paused = bool(self._playback.get("paused"))
            duration_sec = self._duration_to_seconds(self._playback.get("duration_sec"))
            target = max(0.0, float(position_sec))
            if duration_sec is not None:
                target = min(target, max(duration_sec - 0.25, 0.0))

        player_name, new_proc = self._spawn_player(audio_url=audio_url, start_offset_sec=target)
        self._terminate_proc(proc)

        with self._lock:
            self._player_proc = new_proc
            self._player_name = player_name
            self._playback["seek_sec"] = target
            self._playback["position_sec"] = target
            self._playback["resumed_at_mono"] = time.monotonic()
            self._playback["paused"] = False
            self._playback["state"] = "playing"
            if was_paused:
                os.kill(new_proc.pid, signal.SIGSTOP)
                self._playback["paused"] = True
                self._playback["state"] = "paused"
                self._playback["resumed_at_mono"] = None

        status = self.status()
        self.bus.publish("podcast.playback", status)
        return status

    def skip(self, delta_sec: float) -> dict:
        with self._lock:
            proc = self._player_proc
            if proc is None or proc.poll() is not None:
                raise ValueError("No active podcast playback")
            current = self._position_sec_locked()
        return self.seek(current + float(delta_sec))

    # ── Internals ─────────────────────────────────────────────────────

    def _load_state(self) -> None:
        with self._lock:
            if not _PODCASTS_STATE_FILE.exists():
                self._subscriptions = []
                return
            try:
                data = json.loads(_PODCASTS_STATE_FILE.read_text())
            except Exception as exc:
                log.warning("PodcastService: failed to load state: %s", exc)
                self._subscriptions = []
                return
            subs = data.get("subscriptions")
            self._subscriptions = list(subs) if isinstance(subs, list) else []

    def _save_state_locked(self) -> None:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        data = {
            "updated_at": time.time(),
            "subscriptions": self._subscriptions,
        }
        _PODCASTS_STATE_FILE.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _stable_id(feed_url: str) -> str:
        return hashlib.sha1(feed_url.encode("utf-8")).hexdigest()[:12]

    def _fetch_feed(self, feed_url: str) -> dict:
        req = urllib.request.Request(feed_url, headers={"User-Agent": "desktop-assistant/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        root = ET.fromstring(raw)
        channel = root.find("channel")
        if channel is None:
            channel = root.find("./{http://www.w3.org/2005/Atom}feed")
        if channel is None:
            raise ValueError("Invalid feed: no channel/feed element")

        def _txt(elem: Optional[ET.Element], default: str = "") -> str:
            if elem is None or elem.text is None:
                return default
            return elem.text.strip()

        title = _txt(channel.find("title"))
        author = _txt(channel.find(f"{_ITUNES_NS}author")) or _txt(channel.find("author"))
        artwork_url = ""
        image = channel.find("image")
        if image is not None:
            artwork_url = _txt(image.find("url"))
        itunes_image = channel.find(f"{_ITUNES_NS}image")
        if itunes_image is not None and itunes_image.attrib.get("href"):
            artwork_url = itunes_image.attrib.get("href", "")

        episodes: list[dict] = []
        for idx, item in enumerate(channel.findall("item")):
            ep_title = _txt(item.find("title"), default=f"Episode {idx + 1}")
            guid = _txt(item.find("guid"))
            pub = _txt(item.find("pubDate"))
            duration = _txt(item.find(f"{_ITUNES_NS}duration"))
            duration_sec = self._duration_to_seconds(duration)

            enclosure = item.find("enclosure")
            audio_url = enclosure.attrib.get("url", "") if enclosure is not None else ""
            if not audio_url:
                # Some feeds place URL in <link>
                audio_url = _txt(item.find("link"))
            if not audio_url:
                continue

            ep_id = guid or self._stable_id(f"{audio_url}|{ep_title}")
            episodes.append({
                "id": ep_id,
                "title": ep_title,
                "published": pub,
                "duration": duration,
                "duration_sec": duration_sec,
                "audio_url": audio_url,
            })

        return {
            "title": title,
            "author": author,
            "artwork_url": artwork_url,
            "episodes": episodes,
        }

    def _build_player_command(self, audio_url: str, start_offset_sec: float = 0.0) -> tuple[str, list[str]]:
        start_offset_sec = max(0.0, float(start_offset_sec))
        start_flag = f"{start_offset_sec:.3f}"
        if shutil.which("mpv"):
            cmd = ["mpv", "--no-video", "--really-quiet", "--no-terminal"]
            if start_offset_sec > 0:
                cmd.append(f"--start={start_flag}")
            cmd.append(audio_url)
            return "mpv", cmd
        if shutil.which("ffplay"):
            cmd = ["ffplay", "-nodisp", "-autoexit", "-loglevel", "error"]
            if start_offset_sec > 0:
                cmd.extend(["-ss", start_flag])
            cmd.append(audio_url)
            return "ffplay", cmd
        if shutil.which("cvlc"):
            cmd = ["cvlc", "--intf", "dummy", "--play-and-exit"]
            if start_offset_sec > 0:
                cmd.extend(["--start-time", str(int(start_offset_sec))])
            cmd.append(audio_url)
            return "cvlc", cmd
        raise RuntimeError("No supported podcast player found (install mpv, ffplay, or vlc)")

    def _spawn_player(self, audio_url: str, start_offset_sec: float = 0.0) -> tuple[str, subprocess.Popen]:
        player_name, cmd = self._build_player_command(audio_url=audio_url, start_offset_sec=start_offset_sec)
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
        except Exception as exc:
            raise RuntimeError(f"Failed to start {player_name}: {exc}") from exc
        return player_name, proc

    @staticmethod
    def _terminate_proc(proc: Optional[subprocess.Popen]) -> None:
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def _position_sec_locked(self) -> float:
        seek = float(self._playback.get("seek_sec") or 0.0)
        resumed_at = self._playback.get("resumed_at_mono")
        paused = bool(self._playback.get("paused"))
        pos = seek
        if not paused and resumed_at is not None:
            pos = seek + max(0.0, time.monotonic() - float(resumed_at))
        dur = self._duration_to_seconds(self._playback.get("duration_sec"))
        if dur is not None:
            pos = min(pos, max(dur, 0.0))
        return max(0.0, pos)

    @staticmethod
    def _duration_to_seconds(raw: object) -> Optional[float]:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            val = float(raw)
            return val if val >= 0 else None
        txt = str(raw).strip()
        if not txt:
            return None
        if txt.isdigit():
            return float(int(txt))
        parts = txt.split(":")
        try:
            nums = [int(p) for p in parts]
        except Exception:
            return None
        if len(nums) == 3:
            return float(nums[0] * 3600 + nums[1] * 60 + nums[2])
        if len(nums) == 2:
            return float(nums[0] * 60 + nums[1])
        return None
