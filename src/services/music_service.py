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

import logging
import os
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

_PIANOBAR_CONFIG_DIR = Path.home() / ".config" / "pianobar"
_PIANOBAR_CONFIG    = _PIANOBAR_CONFIG_DIR / "config"
_PIANOBAR_FIFO      = _PIANOBAR_CONFIG_DIR / "ctl"

_RE_SONG    = re.compile(r'\|>\s+"(.+?)"\s+by\s+"(.+?)"\s+on\s+"(.+?)"')
_RE_STATION = re.compile(r'^\s*(\d+)\)\s+(.+)$')
_RE_SELECT  = re.compile(r'[Ss]elect station|[Cc]hoose station')


class MusicService(Service):
    """Pandora music playback managed via a pianobar subprocess."""

    name = "music"

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
        self._state: str = "stopped"          # stopped | playing | paused
        self._current_song: dict = {}
        self._stations: list[dict] = []
        self._pending_station: Optional[int] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._unsubs: list = []

    # ── Properties ────────────────────────────────────────────────────

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def current_song(self) -> dict:
        with self._lock:
            return dict(self._current_song)

    @property
    def stations(self) -> list[dict]:
        with self._lock:
            return list(self._stations)

    @property
    def is_configured(self) -> bool:
        return _PIANOBAR_CONFIG.exists()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def on_start(self) -> None:
        if not self._enabled:
            log.info("MusicService disabled via config")
            return
        self._unsubs += [
            self.bus.subscribe("music.play",         self._on_play),
            self.bus.subscribe("music.stop",         self._on_stop),
            self.bus.subscribe("music.next",         self._on_next),
            self.bus.subscribe("music.pause",        self._on_pause),
            self.bus.subscribe("music.thumbs_up",    self._on_thumbs_up),
            self.bus.subscribe("music.thumbs_down",  self._on_thumbs_down),
            self.bus.subscribe("music.set_station",  self._on_set_station),
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
        self._patch_pianobar_config()

        with self._lock:
            self._pending_station = station_id
            self._stations = []

        self._stop_event.clear()
        try:
            self._proc = subprocess.Popen(
                ["pianobar"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                bufsize=0,           # unbuffered binary so we see partial lines
            )
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
                proc.terminate()
        self._proc = None
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
        """Add fifo line to pianobar config if not already present."""
        try:
            content = _PIANOBAR_CONFIG.read_text()
            if "fifo" not in content:
                with _PIANOBAR_CONFIG.open("a") as f:
                    f.write(f"\nfifo = {_PIANOBAR_FIFO}\n")
                log.info("Added fifo line to pianobar config")
        except Exception:
            log.exception("Could not patch pianobar config")

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

    # ── Output parser ─────────────────────────────────────────────────

    def _read_output(self) -> None:
        """Parse pianobar stdout; update state and publish bus events.

        pianobar's 'Select station:' prompt has no trailing newline, so we read
        byte-by-byte and process both complete lines (ending in \\n) and partial
        lines (detected by matching a known prompt suffix).
        """
        stations_buf: list[dict] = []
        buf = b""

        while True:
            if self._stop_event.is_set():
                break
            chunk = self._proc.stdout.read(1)
            if not chunk:
                break
            buf += chunk

            # Process complete lines
            if b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace").rstrip()
                self._process_line(line, stations_buf)
                continue

            # Detect the station-select prompt (no trailing newline)
            decoded = buf.decode("utf-8", errors="replace")
            if _RE_SELECT.search(decoded):
                log.debug("pianobar: %s", decoded.strip())
                if stations_buf:
                    with self._lock:
                        self._stations = list(stations_buf)
                        pending = self._pending_station
                        self._pending_station = None
                    self.bus.publish("music.stations_updated", {"stations": list(stations_buf)})
                    stations_buf.clear()
                    target = pending if pending is not None else 0
                    log.info("Selecting station %d", target)
                    time.sleep(0.3)
                    self._send(str(target))
                buf = b""

        # Process any remaining buffer content
        if buf:
            line = buf.decode("utf-8", errors="replace").rstrip()
            if line:
                self._process_line(line, stations_buf)

        # Process ended
        with self._lock:
            self._state = "stopped"
        self.bus.publish("music.state_changed", {"state": "stopped"})
        log.info("pianobar process ended")

    def _process_line(self, line: str, stations_buf: list) -> None:
        """Handle a single decoded line from pianobar stdout."""
        log.debug("pianobar: %s", line)

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
                self._state = "playing"
            self.bus.publish("music.song_changed", song)
            self.bus.publish("music.state_changed", {"state": "playing"})
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
