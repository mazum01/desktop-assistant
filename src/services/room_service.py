"""
Room awareness service.

Tracks which room VERA is physically located in. Persists the room name
(and a visual scene signature) across restarts. When sustained visual
divergence from the known baseline is detected — indicating VERA may have
been moved — VERA speaks a prompt asking the user to confirm the room.

The visual signature is a coarse brightness histogram over a downsampled
frame, which captures macro room characteristics (lighting level, wall
brightness) without being fooled by foreground objects that change
frequently.

Topics subscribed:
    room.set      {"name": str}    — explicitly set the current room

Topics published:
    room.updated  {"name": str}    — fired whenever the room name changes
    av.say        {"text": str}    — spoken prompts (unknown room, divergence)
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).parents[2] / "config" / "room_state.json"

# Visual change-detection tunables
_SAMPLE_INTERVAL_S: float = 300.0   # check scene every 5 minutes
_CONSEC_DIVERGED: int = 3           # 3 consecutive diverged samples ≈ 15 min sustained change
_SIMILARITY_THRESH: float = 0.80    # Bhattacharyya coefficient below this = "looks different"
_PROMPT_COOLDOWN_S: float = 1800.0  # at most one room-change prompt every 30 minutes


class RoomService(Service):
    """
    Tracks which room VERA is in.

    On startup, speaks the current room name or asks "Which room am I in?"
    when no room has been assigned. Periodically compares the live camera
    scene against a stored visual baseline; if the scene looks consistently
    different over an extended period, VERA prompts the user to confirm
    whether the room has changed.
    """

    name = "room"
    tick_seconds = 0  # driven by internal sampling thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        state_path: Path = _STATE_PATH,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._state_path = state_path
        self._room_name: Optional[str] = None
        self._baseline_sig: Optional[np.ndarray] = None
        self._consec_diverged: int = 0
        self._last_prompt_ts: float = float("-inf")
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()
        self._timer_thread: Optional[threading.Thread] = None

    # ── Properties ────────────────────────────────────────────────────

    @property
    def room_name(self) -> Optional[str]:
        with self._lock:
            return self._room_name

    # ── Service lifecycle ─────────────────────────────────────────────

    def on_start(self) -> None:
        self._load_state()
        self.bus.subscribe("room.set", self._on_set)
        self._stop_evt.clear()
        self._timer_thread = threading.Thread(
            target=self._sample_loop, name="room-sampler", daemon=True
        )
        self._timer_thread.start()
        threading.Thread(
            target=self._announce_on_start, name="room-announce", daemon=True
        ).start()
        log.info("RoomService started (room=%s)", self._room_name or "unknown")

    def run_tick(self) -> None:
        pass  # driven by _sample_loop thread

    def on_stop(self) -> None:
        self._stop_evt.set()
        if self._timer_thread is not None:
            self._timer_thread.join(timeout=5.0)
        log.info("RoomService stopped")

    # ── Bus handlers ──────────────────────────────────────────────────

    def _on_set(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        name = str(payload.get("name", "")).strip()
        if not name:
            return

        with self._lock:
            self._room_name = name
            # Capture a new visual baseline so divergence resets cleanly
            sig = self._capture_signature()
            if sig is not None:
                self._baseline_sig = sig
            self._consec_diverged = 0
            # Suppress the next divergence prompt to avoid immediate re-fire
            self._last_prompt_ts = time.monotonic()

        self._save_state()
        if self.bus:
            self.bus.publish("room.updated", {"name": name})
            self.bus.publish("av.say", {"text": f"OK, I'm in the {name}."})
        log.info("RoomService: room set to %r", name)

    # ── Internal ──────────────────────────────────────────────────────

    def _announce_on_start(self) -> None:
        time.sleep(6.0)  # allow AV service to finish initialising
        with self._lock:
            room = self._room_name
        if room:
            if self.bus:
                self.bus.publish("av.say", {"text": f"I'm in the {room}."})
        else:
            if self.bus:
                self.bus.publish(
                    "av.say",
                    {"text": "Which room am I in? You can tell me with 'vera room set <name>'."},
                )

    def _sample_loop(self) -> None:
        """Background thread: sample scene every _SAMPLE_INTERVAL_S seconds."""
        while not self._stop_evt.wait(timeout=_SAMPLE_INTERVAL_S):
            self._check_scene()

    def _check_scene(self) -> None:
        """Sample the camera frame; compare with baseline; prompt if diverged long enough."""
        sig = self._capture_signature()
        if sig is None:
            return

        should_prompt = False
        room_name: Optional[str] = None

        with self._lock:
            if self._baseline_sig is None:
                # Establish baseline on first successful capture
                self._baseline_sig = sig.copy()
                log.debug("RoomService: established initial visual baseline")
                return

            similarity = _bhattacharyya(self._baseline_sig, sig)
            log.debug(
                "RoomService: scene similarity=%.3f (threshold=%.2f)",
                similarity,
                _SIMILARITY_THRESH,
            )

            if similarity < _SIMILARITY_THRESH:
                self._consec_diverged += 1
                log.info(
                    "RoomService: scene diverged (%d/%d)",
                    self._consec_diverged,
                    _CONSEC_DIVERGED,
                )
            else:
                self._consec_diverged = 0

            elapsed_since_prompt = time.monotonic() - self._last_prompt_ts
            if (
                self._consec_diverged >= _CONSEC_DIVERGED
                and elapsed_since_prompt >= _PROMPT_COOLDOWN_S
            ):
                should_prompt = True
                room_name = self._room_name
                self._last_prompt_ts = time.monotonic()

        if should_prompt:
            self._prompt_room_confirmation(room_name)

    def _prompt_room_confirmation(self, room: Optional[str]) -> None:
        if room:
            text = (
                f"I notice my surroundings look quite different. "
                f"Am I still in the {room}? "
                f"You can update my location with 'vera room set'."
            )
        else:
            text = (
                "Which room am I in? "
                "You can tell me with 'vera room set <name>'."
            )
        log.info("RoomService: prompting room confirmation (current=%s)", room)
        if self.bus:
            self.bus.publish("av.say", {"text": text})

    def _capture_signature(self) -> Optional[np.ndarray]:
        """Grab the latest camera frame and compute a brightness histogram signature."""
        if self._vision_svc is None:
            return None
        try:
            frame = self._vision_svc.latest_frame()
            if frame is None:
                return None
            return _compute_signature(frame)
        except Exception:
            log.debug("RoomService: frame capture failed", exc_info=True)
            return None

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text())
                self._room_name = data.get("name") or None
                sig_list = data.get("signature")
                if sig_list and isinstance(sig_list, list):
                    self._baseline_sig = np.array(sig_list, dtype=float)
                log.info("RoomService: loaded state (room=%s)", self._room_name)
        except Exception as exc:
            log.warning("RoomService: could not load state: %s", exc)

    def _save_state(self) -> None:
        try:
            with self._lock:
                data: dict = {
                    "name": self._room_name,
                    "signature": (
                        self._baseline_sig.tolist()
                        if self._baseline_sig is not None
                        else None
                    ),
                }
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("RoomService: could not save state: %s", exc)


# ── Pure signal-processing helpers ────────────────────────────────────────────


def _compute_signature(frame: np.ndarray) -> np.ndarray:
    """
    Compute a normalized 32-bin brightness histogram as a visual room signature.

    The frame is aggressively downsampled before histogramming so that
    foreground objects (people, furniture) have little influence — only the
    overall luminance distribution of the scene is captured.
    """
    # Downsample to ~80×60 from a 640×480 source
    small = frame[::8, ::8]
    if small.ndim == 3:
        gray = small.mean(axis=2)
    else:
        gray = small.astype(float)

    hist, _ = np.histogram(gray.ravel(), bins=32, range=(0.0, 256.0))
    total = hist.sum()
    if total > 0:
        return hist.astype(float) / total
    return hist.astype(float)


def _bhattacharyya(h1: np.ndarray, h2: np.ndarray) -> float:
    """Bhattacharyya coefficient: 1.0 = identical distributions, 0.0 = orthogonal."""
    return float(np.sum(np.sqrt(h1 * h2)))
