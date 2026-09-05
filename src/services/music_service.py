"""
Music service — Pandora playback via pianobar.

pianobar (https://github.com/PromyLOPh/pianobar) is a console Pandora
client.  Commands are sent through a named FIFO; stdout is parsed for
station lists and now-playing metadata.

Pre-requisites
--------------
  sudo apt install pianobar
  # Create ~/.config/pianobar/config with:
  #   user = your@email.com
  #   password = yourpassword

Topics subscribed
-----------------
music.play          {} or {"station_id": int}  — start or resume
music.stop          {}  — stop playback
music.next          {}  — skip to next song
music.pause         {}  — toggle pause/resume
music.thumbs_up     {}  — love current song
music.thumbs_down   {}  — ban current song (skips)
music.set_station   {"station_id": int}  — switch station

Topics published
----------------
music.state_changed     {"state": "playing"|"paused"|"stopped"}
music.song_changed      {"title": str, "artist": str, "station": str}
music.stations_updated  {"stations": [{"id": int, "name": str}, ...]}
music.error             {"reason": str}
"""

from __future__ import annotations

import json
import logging
import os
import pty
import re
import select
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from src.audio import volume_state
from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

def _get_default_sink() -> str:
    """Get the default audio sink ID, or @DEFAULT_AUDIO_SINK@ if not discoverable.

    Prefers the DA Equalizer filter-chain sink (the node the user's
    persisted volume actually applies to) when the PipeWire EQ is active.
    That node lives under wpctl status's "Filters:" section, not
    "Sinks:", so the naive "first row under Sinks:" scan below always
    picked the raw reSpeaker hardware sink instead — which
    pipewire_eq._pin_hardware_sink_volume() unconditionally re-pins to
    100% on every restart/preset change. That mismatch was the volume
    reset regression: this function never touched the sink the user's
    volume was persisted against.
    """
    try:
        from src.audio import pipewire_eq
        eq_sink_id = pipewire_eq.get_active_sink_id()
        if eq_sink_id:
            return eq_sink_id
    except Exception as exc:
        log.debug("Failed to resolve DA EQ sink, falling back: %s", exc)
    try:
        # List all sinks and get the first one (most likely to be default)
        out = subprocess.check_output(
            ["wpctl", "status"], text=True
        )
        lines = out.split('\n')
        in_sinks_section = False
        for line in lines:
            if 'Sinks:' in line:
                in_sinks_section = True
                continue
            if in_sinks_section:
                # Look for sink ID lines like "│  *   53. reSpeaker Flex..."
                # or "│      53. reSpeaker Flex..."
                line_stripped = line.strip()
                if not line_stripped or 'Source' in line:
                    break
                # Extract sink ID: match digits followed by a period
                parts = line_stripped.split()
                for i, part in enumerate(parts):
                    if part.isdigit() or (part.rstrip('.').isdigit() and part.endswith('.')):
                        sink_id = part.rstrip('.')
                        if sink_id.isdigit():
                            return sink_id
        return "@DEFAULT_AUDIO_SINK@"
    except Exception as e:
        log.debug("Failed to discover sink: %s", e)
        return "@DEFAULT_AUDIO_SINK@"

_CACHED_SINK_ID: Optional[str] = None

def _sink_id_is_live(sink_id: str) -> bool:
    """True if *sink_id* still refers to an existing PipeWire node."""
    if not sink_id or not sink_id.isdigit():
        # Symbolic targets like @DEFAULT_AUDIO_SINK@ are always resolvable.
        return bool(sink_id)
    try:
        r = subprocess.run(
            ["wpctl", "get-volume", sink_id],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        # Can't prove it's dead — assume live rather than thrashing discovery.
        return True


def invalidate_sink_cache() -> None:
    """Drop the cached sink ID so the next call re-discovers it.

    Must be called whenever the filter-chain is restarted: PipeWire assigns
    a brand-new node ID to the recreated ``effect_input.da_eq`` sink, so any
    previously cached ID is guaranteed to be stale.
    """
    global _CACHED_SINK_ID
    _CACHED_SINK_ID = None


def _get_sink_id() -> str:
    """Get cached sink ID or discover it.

    Only the DA-EQ-resolved sink ID is cached; the raw-hw-sink fallback
    (used when the EQ filter-chain isn't up yet) is deliberately never
    cached, so a transient startup race doesn't pin this service to the
    wrong node for its entire lifetime.

    The cached ID is re-validated before use. Saving an EQ profile restarts
    filter-chain.service, which destroys and recreates the EQ sink under a
    *new* node ID — the cache previously had no invalidation at all, so
    every subsequent volume call targeted a node that no longer existed.
    ``wpctl set-volume`` on a dead node fails silently from the caller's
    perspective (the API still reported ok), which is exactly why the web
    GUI volume slider stopped working after saving EQ settings.
    """
    global _CACHED_SINK_ID
    if _CACHED_SINK_ID is not None:
        if _sink_id_is_live(_CACHED_SINK_ID):
            return _CACHED_SINK_ID
        log.info("Cached sink %s is gone (filter-chain restart?) — rediscovering",
                 _CACHED_SINK_ID)
        _CACHED_SINK_ID = None
    sink_id = _get_default_sink()
    try:
        from src.audio import pipewire_eq
        if pipewire_eq.get_active_sink_id() == sink_id:
            _CACHED_SINK_ID = sink_id
    except Exception:
        pass
    return sink_id

_PIANOBAR_CONFIG_DIR  = Path.home() / ".config" / "pianobar"
_PIANOBAR_CONFIG      = _PIANOBAR_CONFIG_DIR / "config"
_PIANOBAR_FIFO        = _PIANOBAR_CONFIG_DIR / "ctl"
_PIANOBAR_EVENT_SCRIPT = _PIANOBAR_CONFIG_DIR / "da-event.sh"
_PIANOBAR_META_JSON   = _PIANOBAR_CONFIG_DIR / "da-meta.json"

_DA_STATE_DIR         = Path.home() / ".config" / "desktop-assistant"
_MUSIC_EQ_STATE_FILE  = _DA_STATE_DIR / "music_eq_preset.txt"

_RE_SONG           = re.compile(r'\|>\s+"(.+?)"\s+by\s+"(.+?)"\s+on\s+"(.+?)"')
_RE_STATION        = re.compile(r'^\s*(\d+)\)\s+(?:[qQ]\s+)?(.+?)$')
_RE_SELECT         = re.compile(r'[Ss]elect station|[Cc]hoose station')
_RE_LOGIN_FAIL     = re.compile(r'Login\.\.\.\s*(Network error|Wrong username|wrong password|Error)', re.IGNORECASE)
_RE_ANSI           = re.compile(r'\x1b\[[0-9;?]*[a-zA-Z]')
# pianobar auto-resume line: "|>  Station "Name" (stationId)"
_RE_STATION_RESUME = re.compile(r'\|>\s+Station\s+"(.+?)"')
# Progress bar: "# -MM:SS/MM:SS" — remaining time / total time
_RE_PROGRESS       = re.compile(r'^#\s+[-]?(\d+):(\d+)/(\d+):(\d+)')


class MusicService(Service):
    """Pandora music playback managed via a pianobar subprocess."""

    name = "music"

    EQ_PRESETS = ["flat", "bass_boost", "treble_boost", "vocal", "loudness", "warm", "custom"]

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        enabled: bool = True,
        announce_song_changes: bool = False,
    ) -> None:
        super().__init__(bus=bus)
        self._enabled = enabled
        self._announce_songs = announce_song_changes
        self._proc: Optional[subprocess.Popen] = None
        self._pty_master: Optional[int] = None
        self._state: str = "stopped"          # stopped | playing | paused
        self._current_song: dict = {}
        self._stations: list[dict] = []
        self._pending_station: Optional[int] = None
        self._got_stations: bool = False
        self._resuming_station_name: str = ""
        self._current_station_id: Optional[int] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._unsubs: list = []
        # Progress tracking
        self._elapsed_sec: int = 0
        self._duration_sec: int = 0
        # Album art / metadata
        self._song_album: str = ""
        self._song_cover_art: str = ""
        # EQ preset (flat = no filtering)
        self._eq_preset: str = "flat"

    # ── Properties ────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def current_song(self) -> dict:
        with self._lock:
            return {
                **self._current_song,
                "album":              self._song_album,
                "cover_art_url":      self._song_cover_art,
                "elapsed_sec":        self._elapsed_sec,
                "duration_sec":       self._duration_sec,
                "current_station_id": self._current_station_id,
            }

    @property
    def stations(self) -> list[dict]:
        with self._lock:
            return list(self._stations)

    @property
    def is_configured(self) -> bool:
        return _PIANOBAR_CONFIG.exists()

    @property
    def volume(self) -> int:
        """Return current system volume as 0–100, or -1 on error."""
        try:
            sink_id = _get_sink_id()
            out = subprocess.check_output(
                ["wpctl", "get-volume", sink_id], text=True
            )
            # Output: "Volume: 0.42" or "Volume: 0.42 [MUTED]"
            val = float(out.split()[1])
            return round(val * 100)
        except Exception:
            return -1

    @property
    def muted(self) -> bool:
        """Return True when the default sink is muted."""
        try:
            sink_id = _get_sink_id()
            out = subprocess.check_output(
                ["wpctl", "get-volume", sink_id], text=True
            )
            return "[MUTED]" in out
        except Exception:
            return False

    def set_volume(self, level: int) -> None:
        """Set system volume to level (0–100)."""
        level = max(0, min(100, level))
        try:
            sink_id = _get_sink_id()
            r = subprocess.run(
                ["wpctl", "set-volume", sink_id, f"{level}%"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                # The sink vanished between discovery and use (filter-chain
                # restart races a volume change). Rediscover once and retry
                # rather than silently reporting success to the caller.
                log.info("set_volume: sink %s rejected write (%s) — retrying",
                         sink_id, (r.stderr or "").strip())
                invalidate_sink_cache()
                sink_id = _get_sink_id()
                r = subprocess.run(
                    ["wpctl", "set-volume", sink_id, f"{level}%"],
                    capture_output=True, text=True,
                )
                if r.returncode != 0:
                    log.error("set_volume(%d) failed on sink %s: %s",
                              level, sink_id, (r.stderr or "").strip())
                    return
            # Persist volume to survive daemon restarts. Only after a
            # confirmed successful write, so a failed call can't poison the
            # persisted level.
            volume_state.save_volume(level)
        except Exception:
            log.exception("set_volume(%d) failed", level)

    def set_muted(self, muted: bool) -> None:
        """Mute/unmute the default sink."""
        try:
            sink_id = _get_sink_id()
            subprocess.run(
                ["wpctl", "set-mute", sink_id, "1" if muted else "0"],
                check=True,
            )
        except Exception:
            log.exception("set_muted(%s) failed", muted)

    @property
    def eq_preset(self) -> str:
        return self._eq_preset

    def set_eq_preset(self, preset: str) -> None:
        """Set EQ preset and notify AVService via the bus."""
        if preset not in self.EQ_PRESETS:
            log.warning("Unknown EQ preset: %r", preset)
            return
        self._eq_preset = preset
        self.bus.publish("av.set_eq_preset", {"preset": preset})
        try:
            _DA_STATE_DIR.mkdir(parents=True, exist_ok=True)
            _MUSIC_EQ_STATE_FILE.write_text(preset)
        except Exception as exc:
            log.warning("MusicService: failed to persist EQ preset: %s", exc)
        log.info("EQ preset changed to %r", preset)

    def mark_eq_custom(self) -> None:
        """Record that the active EQ is a user-defined custom curve.

        Unlike `set_eq_preset()`, this does NOT re-publish `av.set_eq_preset`
        (the caller — the `/api/music/eq/custom` route — already published
        `av.set_custom_eq` with the actual band values; re-publishing here
        would tell AVService to switch to a *named* "custom" preset with no
        bands, clobbering what was just set). Only updates the tracked
        preset name (for `eq_preset`/`/api/music/status`) and persists it so
        the "custom" selection survives daemon restarts.
        """
        self._eq_preset = "custom"
        try:
            _DA_STATE_DIR.mkdir(parents=True, exist_ok=True)
            _MUSIC_EQ_STATE_FILE.write_text("custom")
        except Exception as exc:
            log.warning("MusicService: failed to persist EQ preset: %s", exc)

    # ── Lifecycle ─────────────────────────────────────────────────────

    def on_start(self) -> None:
        if not self._enabled:
            log.info("MusicService disabled via config")
            return
        # Restore persisted EQ preset (in-memory only — AVService handles the audio side).
        if _MUSIC_EQ_STATE_FILE.exists():
            try:
                saved = _MUSIC_EQ_STATE_FILE.read_text().strip()
                if saved in self.EQ_PRESETS:
                    self._eq_preset = saved
                    log.info("MusicService: restored EQ preset %r", saved)
            except Exception as exc:
                log.warning("MusicService: failed to restore EQ preset: %s", exc)
        # Restore persisted volume level
        level = volume_state.load_volume()
        if level is not None:
            self.set_volume(level)
            log.info("MusicService: restored volume to %d%%", level)
        self._unsubs += [
            self.bus.subscribe("music.play",         self._on_play),
            self.bus.subscribe("music.stop",         self._on_stop),
            self.bus.subscribe("music.next",         self._on_next),
            self.bus.subscribe("music.pause",        self._on_pause),
            self.bus.subscribe("music.thumbs_up",    self._on_thumbs_up),
            self.bus.subscribe("music.thumbs_down",  self._on_thumbs_down),
            self.bus.subscribe("music.set_station",  self._on_set_station),
            self.bus.subscribe("music.set_volume",   self._on_set_volume),
        ]
        log.info("MusicService started (enabled=%s)", self._enabled)

    def on_stop(self) -> None:
        self._stop_event.set()
        self._kill_pianobar()
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        log.info("MusicService stopped")

    # ── Bus handlers ──────────────────────────────────────────────────

    def _on_play(self, _t, payload) -> None:
        station_id = None
        if isinstance(payload, dict) and "station_id" in payload:
            station_id = int(payload["station_id"])

        with self._lock:
            state = self._state
            running = self._proc is not None and self._proc.poll() is None

        if state == "paused" and running:
            self._send("p")
        elif running and station_id is not None:
            self._switch_station(station_id)
        elif not running:
            self._start_pianobar(station_id)

    def _on_stop(self, _t, _payload) -> None:
        self._kill_pianobar()

    def _on_next(self, _t, _payload) -> None:
        self._send("n")

    def _on_pause(self, _t, _payload) -> None:
        self._send("p")

    def _on_thumbs_up(self, _t, _payload) -> None:
        self._send("+")

    def _on_thumbs_down(self, _t, _payload) -> None:
        self._send("-")

    def _on_set_station(self, _t, payload) -> None:
        if isinstance(payload, dict) and "station_id" in payload:
            self._switch_station(int(payload["station_id"]))

    def _on_set_volume(self, _t, payload) -> None:
        """Handle ``music.set_volume`` bus event.

        Payload may contain:
        - ``{"level": int}``  — set absolute volume 0–100
        - ``{"delta": int}``  — adjust relative to current (positive = louder)
        """
        if not isinstance(payload, dict):
            return
        if "level" in payload:
            self.set_volume(int(payload["level"]))
        elif "delta" in payload:
            current = self.volume
            if current >= 0:
                self.set_volume(current + int(payload["delta"]))

    # ── pianobar management ───────────────────────────────────────────

    def _start_pianobar(self, station_id: Optional[int] = None) -> None:
        if not self.is_configured:
            log.warning("pianobar not configured — create %s", _PIANOBAR_CONFIG)
            self.bus.publish("music.error", {
                "reason": f"pianobar not configured. Create {_PIANOBAR_CONFIG} "
                          "with user = email and password = pass"
            })
            return

        self._ensure_fifo()
        self._write_event_script()
        self._patch_pianobar_config()

        with self._lock:
            self._pending_station = station_id
            self._stations = []
            self._got_stations = False
            self._resuming_station_name = ""

        self._stop_event.clear()

        # pianobar refuses to produce output unless stdout is a TTY.
        # Allocate a pseudo-terminal so we can capture its output.
        #
        # PULSE_LATENCY_MSEC=500 requests a 500 ms PulseAudio playback buffer.
        # Default (~100 ms) is tight enough that brief CPU spikes (from Hailo
        # inference, camera capture, etc.) cause buffer underruns and choppy
        # audio. 500 ms is imperceptible for streaming music but absorbs any
        # scheduling jitter from other services running on the same cores.
        env = {**os.environ, "PULSE_LATENCY_MSEC": "500"}
        try:
            self._pty_master, slave_fd = pty.openpty()
            self._proc = subprocess.Popen(
                ["pianobar"],
                stdout=slave_fd,
                stderr=slave_fd,
                stdin=slave_fd,
                close_fds=True,
                preexec_fn=os.setsid,
                env=env,
            )
            os.close(slave_fd)
        except FileNotFoundError:
            self.bus.publish("music.error", {"reason": "pianobar not installed"})
            log.error("pianobar executable not found")
            return

        self._reader_thread = threading.Thread(
            target=self._read_output,
            name="pianobar-reader",
            daemon=True,
        )
        self._reader_thread.start()
        log.info("pianobar started (pid=%d)", self._proc.pid)

    def _kill_pianobar(self) -> None:
        proc = self._proc
        if proc and proc.poll() is None:
            self._send("q")
            try:
                proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
        self._proc = None
        if self._pty_master is not None:
            try:
                os.close(self._pty_master)
            except OSError:
                pass
            self._pty_master = None
        with self._lock:
            self._state = "stopped"
            self._current_song = {}
        self.bus.publish("music.state_changed", {"state": "stopped"})

    def _ensure_fifo(self) -> None:
        _PIANOBAR_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if not _PIANOBAR_FIFO.exists():
            try:
                os.mkfifo(str(_PIANOBAR_FIFO))
            except FileExistsError:
                pass

    def _patch_pianobar_config(self) -> None:
        """Add fifo and event_command lines to pianobar config if not already present."""
        try:
            content = _PIANOBAR_CONFIG.read_text()
            updated = False
            if "fifo" not in content:
                with _PIANOBAR_CONFIG.open("a") as f:
                    f.write(f"\nfifo = {_PIANOBAR_FIFO}\n")
                updated = True
            # Remove legacy wrong key if present
            if "eventcommand" in content and "event_command" not in content:
                content = content.replace(
                    f"eventcommand = {_PIANOBAR_EVENT_SCRIPT}",
                    f"event_command = {_PIANOBAR_EVENT_SCRIPT}",
                )
                _PIANOBAR_CONFIG.write_text(content)
                updated = True
            elif "event_command" not in content:
                with _PIANOBAR_CONFIG.open("a") as f:
                    f.write(f"\nevent_command = {_PIANOBAR_EVENT_SCRIPT}\n")
                updated = True
            if updated:
                log.info("Patched pianobar config (fifo/event_command)")
        except Exception:
            log.exception("Could not patch pianobar config")

    def _write_event_script(self) -> None:
        """Write the pianobar event script that captures song metadata.

        pianobar passes the event name as argv[1] and streams key=value pairs
        via stdin (one per line).  Environment variables are NOT used.
        """
        script = (
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"META = '{_PIANOBAR_META_JSON}'\n"
            "ev = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if ev == 'songstart':\n"
            "    fields = {}\n"
            "    for line in sys.stdin:\n"
            "        line = line.rstrip('\\n')\n"
            "        if '=' in line:\n"
            "            k, _, v = line.partition('=')\n"
            "            fields[k.strip()] = v.strip()\n"
            "    data = {\n"
            "        'title':     fields.get('title', ''),\n"
            "        'artist':    fields.get('artist', ''),\n"
            "        'album':     fields.get('album', ''),\n"
            "        'cover_art': fields.get('coverArt', ''),\n"
            "        'duration':  fields.get('songDuration', '0'),\n"
            "        'station':   fields.get('stationName', ''),\n"
            "    }\n"
            "    os.makedirs(os.path.dirname(META), exist_ok=True)\n"
            "    with open(META, 'w') as f:\n"
            "        json.dump(data, f)\n"
        )
        try:
            _PIANOBAR_EVENT_SCRIPT.write_text(script)
            _PIANOBAR_EVENT_SCRIPT.chmod(0o755)
            log.info("Wrote pianobar event script to %s", _PIANOBAR_EVENT_SCRIPT)
        except Exception:
            log.exception("Could not write pianobar event script")

    def _send(self, cmd: str) -> None:
        """Write a command character to the pianobar FIFO (non-blocking)."""
        if not _PIANOBAR_FIFO.exists():
            return

        def _write():
            try:
                # O_NONBLOCK avoids blocking if pianobar hasn't opened its read end yet.
                fd = os.open(str(_PIANOBAR_FIFO), os.O_WRONLY | os.O_NONBLOCK)
                os.write(fd, (cmd + "\n").encode())
                os.close(fd)
            except OSError as exc:
                log.debug("FIFO write failed (%s): %s", cmd.strip(), exc)

        threading.Thread(target=_write, daemon=True).start()

    def _switch_station(self, station_id: int) -> None:
        """Select a station while pianobar is running."""
        self._send(f"s{station_id}")

    def _request_station_list(self) -> None:
        """Send 's' via FIFO to force pianobar to display the station list.

        Used when pianobar auto-resumes without showing the selection prompt
        (pianobar stores the last station in its state file).  Waits briefly
        for pianobar to settle before sending so the FIFO is ready.
        """
        time.sleep(1.2)
        if self._got_stations or self._stop_event.is_set():
            return
        log.info("pianobar auto-resumed without station list; requesting via FIFO")
        self._send("s")

    # ── Output parser ─────────────────────────────────────────────────

    def _read_output(self) -> None:
        """Parse pianobar output (via PTY); update state and publish bus events.

        pianobar writes CRLF line endings, embeds ANSI escape sequences for
        cursor control, and uses CR to overwrite its progress bar in-place.
        The 'Select station:' prompt may or may not have a trailing newline
        depending on whether it was triggered by startup or a FIFO 's' command.
        We therefore detect the SELECT prompt both in complete lines and in the
        rolling partial-line buffer.
        """
        log.info("pianobar reader thread started (master_fd=%s)", self._pty_master)
        stations_buf: list[dict] = []
        buf = b""
        master = self._pty_master

        def _handle_select_prompt() -> None:
            """Commit the accumulated station list and auto-select a station."""
            nonlocal stations_buf
            if not stations_buf:
                return
            with self._lock:
                self._stations = list(stations_buf)
                self._got_stations = True
                pending = self._pending_station
                self._pending_station = None
                resuming = self._resuming_station_name
            self.bus.publish("music.stations_updated",
                             {"stations": list(stations_buf)})
            log.info("Captured %d stations", len(stations_buf))
            # Re-select the previously playing station if no explicit target.
            if pending is None and resuming:
                for s in stations_buf:
                    if s["name"] == resuming:
                        pending = s["id"]
                        break
            stations_buf.clear()
            target = pending if pending is not None else 0
            log.info("Auto-selecting station %d (writing to PTY)", target)
            time.sleep(0.2)
            try:
                os.write(master, f"{target}\n".encode())
            except OSError as exc:
                log.warning("PTY write failed: %s", exc)

        while master is not None and not self._stop_event.is_set():
            try:
                r, _, _ = select.select([master], [], [], 0.5)
            except (OSError, ValueError) as exc:
                log.warning("pianobar reader select failed: %s", exc)
                break
            if not r:
                # Detect process exit while idle
                if self._proc and self._proc.poll() is not None:
                    break
                continue
            try:
                chunk = os.read(master, 4096)
            except OSError as exc:
                log.warning("pianobar reader read failed: %s", exc)
                break
            if not chunk:
                log.warning("pianobar reader: empty read (EOF on PTY)")
                break
            buf += chunk

            # Process all complete lines.
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                decoded = _RE_ANSI.sub("", raw.decode("utf-8", errors="replace"))
                # pianobar uses \r\n line endings; embedded \r overwrites the
                # progress bar. Scan ALL CR-split parts for progress data before
                # discarding them, then keep only the last non-empty part.
                if "\r" in decoded:
                    parts = decoded.rstrip("\r").split("\r")
                    for part in parts:
                        pm = _RE_PROGRESS.match(part.strip())
                        if pm:
                            rem_m, rem_s, tot_m, tot_s = (int(x) for x in pm.groups())
                            total   = tot_m * 60 + tot_s
                            elapsed = total - (rem_m * 60 + rem_s)
                            with self._lock:
                                self._duration_sec = total
                                self._elapsed_sec  = max(0, elapsed)
                    decoded = parts[-1]
                line = decoded.strip()
                if not line:
                    continue
                if _RE_SELECT.search(line):
                    log.debug("pianobar prompt (full line): %s", line)
                    _handle_select_prompt()
                    continue
                self._process_line(line, stations_buf)

            # Detect partial-line prompt (no newline yet).
            tail = _RE_ANSI.sub("", buf.decode("utf-8", errors="replace"))
            # Also scan tail for progress data (progress lines may not have \n).
            if "\r" in tail:
                for part in tail.split("\r"):
                    pm = _RE_PROGRESS.match(part.strip())
                    if pm:
                        rem_m, rem_s, tot_m, tot_s = (int(x) for x in pm.groups())
                        total   = tot_m * 60 + tot_s
                        elapsed = total - (rem_m * 60 + rem_s)
                        with self._lock:
                            self._duration_sec = total
                            self._elapsed_sec  = max(0, elapsed)
            if _RE_SELECT.search(tail):
                log.debug("pianobar prompt (partial): %s", tail.strip())
                _handle_select_prompt()
                buf = b""

        # Process ended
        with self._lock:
            self._state = "stopped"
        self.bus.publish("music.state_changed", {"state": "stopped"})
        log.info("pianobar process ended")

    def _process_line(self, line: str, stations_buf: list) -> None:
        """Handle a single decoded line from pianobar stdout."""
        log.debug("pianobar: %s", line)

        # Login / network failure
        if "Login..." in line or "Get stations..." in line or "Network error" in line:
            log.info("pianobar: %s", line)
        m = _RE_LOGIN_FAIL.search(line)
        if m:
            self.bus.publish("music.error",
                             {"reason": f"pianobar login failed: {m.group(1)}"})
            log.error("pianobar login failed: %s", line)
            return

        # Detect auto-resume: pianobar skips the selection prompt when its
        # state file contains a saved station.  Capture the station name and
        # request the full list via FIFO so the GUI dropdown populates.
        m = _RE_STATION_RESUME.match(line)
        if m:
            with self._lock:
                already = self._got_stations
                self._resuming_station_name = m.group(1)
            if not already:
                threading.Thread(
                    target=self._request_station_list,
                    name="pianobar-station-fetch",
                    daemon=True,
                ).start()
            return

        # Station list entry
        m = _RE_STATION.match(line)
        if m and not line.startswith("|"):
            stations_buf.append({"id": int(m.group(1)), "name": m.group(2).strip()})
            return

        # Now playing
        m = _RE_SONG.search(line)
        if m:
            song = {
                "title":   m.group(1),
                "artist":  m.group(2),
                "station": m.group(3),
            }
            with self._lock:
                self._current_song = song
                self._state        = "playing"
                self._elapsed_sec  = 0
                self._duration_sec = 0
                self._song_album   = ""
                self._song_cover_art = ""
            self.bus.publish("music.song_changed", song)
            self.bus.publish("music.state_changed", {"state": "playing"})
            threading.Thread(
                target=self._load_meta_after_delay,
                name="pianobar-meta-loader",
                daemon=True,
            ).start()
            if self._announce_songs:
                self.bus.publish("av.say", {
                    "text": f"Now playing {song['title']} by {song['artist']}"
                })
            return

        # Paused
        if "|| paused" in line or line.strip() == "|| paused":
            with self._lock:
                self._state = "paused"
            self.bus.publish("music.state_changed", {"state": "paused"})
            return

        # Resumed from pause
        if "|>" in line and "paused" not in line:
            with self._lock:
                if self._state == "paused":
                    self._state = "playing"
                    self.bus.publish("music.state_changed", {"state": "playing"})

    def _load_meta_after_delay(self) -> None:
        """Sleep briefly then read da-meta.json written by the event script."""
        time.sleep(0.8)
        try:
            if not _PIANOBAR_META_JSON.exists():
                return
            data = json.loads(_PIANOBAR_META_JSON.read_text())
            with self._lock:
                self._song_album     = data.get("album", "")
                self._song_cover_art = data.get("cover_art", "")
                dur = data.get("duration", "0")
                try:
                    self._duration_sec = int(dur)
                except (ValueError, TypeError):
                    pass
                # Resolve station id from name
                station_name = data.get("station", "")
                if station_name:
                    for s in self._stations:
                        if s["name"] == station_name:
                            self._current_station_id = s["id"]
                            break
                    # Also update the song's station field if it was blank
                    if station_name and not self._current_song.get("station"):
                        self._current_song["station"] = station_name
        except Exception:
            log.exception("Failed to load pianobar metadata from %s", _PIANOBAR_META_JSON)
