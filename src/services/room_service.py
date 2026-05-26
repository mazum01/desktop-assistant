"""
Room awareness service.

Tracks which room VERA is physically located in. Persists the room name
and a multi-angle visual signature across restarts. When sustained visual
divergence from the known baseline is detected — indicating VERA may have
been moved to a different room — VERA speaks a prompt asking the user to
confirm.

Signature methodology
---------------------
A room signature combines two independent histogram signals:

1. **Multi-angle brightness histogram** (32 bins, normalised):
   The servo sweeps to _SWEEP_ANGLES (default: left, center, right).
   At each position a frame is downsampled to ~80×60 px and a normalised
   brightness histogram computed.  The per-angle histograms are averaged,
   making the signature view-invariant — rotating the head to face a
   different wall will not by itself trigger a false divergence alarm.

2. **Depth histogram** (16 bins over [0, _DEPTH_MAX_M] m, normalised):
   The latest depth map from either the stereo or monocular depth service
   is cached via bus subscription.  If depth data is available, a histogram
   of per-pixel depth values is included in the signature.  This captures
   room *geometry* (how far away the walls are), which is far more stable
   than lighting.

Comparison uses the Bhattacharyya coefficient on each component:
    score = 0.6 × B(brightness) + 0.4 × B(depth)   [both available]
    score = B(brightness)                            [depth unavailable]

Thresholds and timing are tunable via module-level constants.

Persistence
-----------
Room name and signature are stored in ``config/room_state.json`` using an
atomic write (write to .json.tmp → replace) to survive unclean shutdowns.

Topics subscribed:
    room.set          {"name": str}     — explicitly set the current room
    motion.position   {"angle": float}  — track current servo angle
    vision.depth_map  {…}               — cache latest stereo depth map
    vision.mono_depth_map {…}           — cache latest mono depth map

Topics published:
    room.updated   {"name": str}   — fired whenever the room name changes
    av.say         {"text": str}   — spoken prompts (unknown room, divergence)
    motion.pan_to  {"angle", …}    — servo pan during signature sweep
    tracking.set_face_tracking {"enabled": bool}  — paused during sweep
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
_SIMILARITY_THRESH: float = 0.80    # combined score below this = "looks different"
_PROMPT_COOLDOWN_S: float = 1800.0  # at most one room-change prompt every 30 minutes

# Low-light skip: if mean brightness of all sweep frames is below this
# (scale 0–255), the sample is inconclusive — skip it without incrementing
# or resetting the divergence counter.  Prevents false alarms when the lights
# are simply turned off.
_LOW_LIGHT_THRESH: float = 15.0

# Panoramic sweep tunables
_SWEEP_ANGLES: tuple[float, ...] = (135.0, 175.0, 215.0)  # L / centre / R (matches default soft limits)
_SWEEP_SETTLE_S: float = 1.0        # seconds to wait after pan before capturing frame

# Depth histogram tunables
_DEPTH_BINS: int = 16
_DEPTH_MAX_M: float = 6.0
_DEPTH_MAX_AGE_S: float = 30.0      # ignore depth data older than this

# Brightness weight vs depth weight when both are available
_W_BRIGHTNESS: float = 0.6
_W_DEPTH: float = 0.4


class RoomService(Service):
    """
    Tracks which room VERA is in.

    On startup, speaks the current room name or asks "Which room am I in?"
    when no room has been assigned. Periodically captures a multi-angle
    panoramic signature and compares it to the stored baseline; if the
    signature looks consistently different over an extended period, VERA
    prompts the user to confirm whether the room has changed.
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

        # Room state
        self._room_name: Optional[str] = None
        self._baseline_brightness: Optional[np.ndarray] = None  # 32-bin
        self._baseline_depth: Optional[np.ndarray] = None        # 16-bin or None

        # Divergence tracking
        self._consec_diverged: int = 0
        self._last_prompt_ts: float = float("-inf")

        # Current servo angle (updated via motion.position subscription)
        self._current_angle: Optional[float] = None

        # Latest depth map cache (updated via vision.depth_map / vision.mono_depth_map)
        self._depth_lock = threading.Lock()
        self._latest_depth_arr: Optional[np.ndarray] = None
        self._latest_depth_ts: float = float("-inf")

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
        self.bus.subscribe("room.set",               self._on_set)
        self.bus.subscribe("motion.position",        self._on_motion_position)
        self.bus.subscribe("vision.depth_map",       self._on_depth_map)
        self.bus.subscribe("vision.mono_depth_map",  self._on_mono_depth_map)
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
            self._consec_diverged = 0
            self._last_prompt_ts = time.monotonic()

        # Capture new baseline asynchronously (sweep takes ~3s)
        threading.Thread(
            target=self._capture_and_store_baseline,
            name="room-baseline",
            daemon=True,
        ).start()

        self._save_state()
        if self.bus:
            self.bus.publish("room.updated", {"name": name})
            self.bus.publish("av.say", {"text": f"OK, I'm in the {name}."})
        log.info("RoomService: room set to %r", name)

    def _on_motion_position(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            self._current_angle = float(payload["angle"])

    def _on_depth_map(self, _topic, payload) -> None:
        """Cache stereo depth map (metres)."""
        if not isinstance(payload, dict):
            return
        try:
            depth_m = payload.get("depth_m")
            if depth_m is None:
                return
            arr = np.array(depth_m, dtype=float)
            with self._depth_lock:
                self._latest_depth_arr = arr
                self._latest_depth_ts = time.monotonic()
        except Exception:
            log.debug("RoomService: depth_map parse failed", exc_info=True)

    def _on_mono_depth_map(self, _topic, payload) -> None:
        """Cache monocular relative depth map; scale to approximate metres."""
        if not isinstance(payload, dict):
            return
        try:
            depth_rel = payload.get("depth_rel")
            scale = float(payload.get("scale_factor") or 0.0)
            if depth_rel is None:
                return
            arr = np.array(depth_rel, dtype=float)
            if scale > 0:
                arr = arr * scale
            # mono depth is relative (inverted: higher = closer), not metric.
            # Use raw relative values mapped into [0, _DEPTH_MAX_M] range.
            if arr.max() > 0:
                arr = arr / arr.max() * _DEPTH_MAX_M
            with self._depth_lock:
                self._latest_depth_arr = arr
                self._latest_depth_ts = time.monotonic()
        except Exception:
            log.debug("RoomService: mono_depth_map parse failed", exc_info=True)

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
        """Capture panoramic signature; compare with baseline; prompt if diverged long enough."""
        sig = self._panoramic_signature()
        if sig is None:
            return

        # Low-light skip: if the scene is too dark to be meaningful, defer
        # judgment without touching the divergence counter.
        if sig.get("mean_brightness", 255.0) < _LOW_LIGHT_THRESH:
            log.debug(
                "RoomService: scene too dark (mean=%.1f) — skipping sample",
                sig["mean_brightness"],
            )
            return

        should_prompt = False
        should_save = False
        room_name: Optional[str] = None

        with self._lock:
            if self._baseline_brightness is None:
                # Establish baseline on first successful capture
                self._baseline_brightness = sig["brightness"].copy()
                self._baseline_depth = sig["depth"].copy() if sig["depth"] is not None else None
                should_save = True
                log.debug("RoomService: established initial visual baseline")
            else:
                similarity = _compare_signatures(
                    self._baseline_brightness,
                    self._baseline_depth,
                    sig["brightness"],
                    sig["depth"],
                )
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

        if should_save:
            self._save_state()
            return

        if should_prompt:
            self._prompt_room_confirmation(room_name)

    def _capture_and_store_baseline(self) -> None:
        """Capture a panoramic baseline signature and persist state."""
        sig = self._panoramic_signature()
        if sig is None:
            return
        with self._lock:
            self._baseline_brightness = sig["brightness"].copy()
            self._baseline_depth = sig["depth"].copy() if sig["depth"] is not None else None
        self._save_state()
        log.info("RoomService: new visual baseline captured after room set")

    def _panoramic_signature(self) -> Optional[dict]:
        """
        Sweep the servo through _SWEEP_ANGLES, capture a frame at each position,
        and return the averaged brightness + depth histograms.

        If no bus/servo is available, falls back to a single-frame capture from
        the current position.  Face tracking is paused for the duration of the
        sweep to prevent servo contention.
        """
        if self._vision_svc is None:
            return None

        original_angle = self._current_angle
        sweep_possible = (self.bus is not None) and (original_angle is not None)

        brightness_hists: list[np.ndarray] = []
        depth_hists: list[np.ndarray] = []

        def _grab_one() -> None:
            try:
                frame = self._vision_svc.latest_frame()
                if frame is not None:
                    brightness_hists.append(_compute_brightness_signature(frame))
            except Exception:
                log.debug("RoomService: frame capture failed", exc_info=True)

            with self._depth_lock:
                age = time.monotonic() - self._latest_depth_ts
                arr = self._latest_depth_arr if age < _DEPTH_MAX_AGE_S else None
            if arr is not None:
                depth_hists.append(_compute_depth_signature(arr))

        if sweep_possible:
            # Pause face tracking to avoid servo contention during the sweep.
            self.bus.publish("tracking.set_face_tracking", {"enabled": False})
            try:
                for angle in _SWEEP_ANGLES:
                    self.bus.publish(
                        "motion.pan_to",
                        {"angle": angle, "override_quiet": True},
                    )
                    time.sleep(_SWEEP_SETTLE_S)
                    _grab_one()
            finally:
                # Restore tracking and return to pre-sweep position.
                self.bus.publish("tracking.set_face_tracking", {"enabled": True})
                self.bus.publish(
                    "motion.pan_to",
                    {"angle": original_angle, "override_quiet": True},
                )
        else:
            # No servo available — single frame from current orientation.
            _grab_one()

        if not brightness_hists:
            return None

        avg_brightness = np.mean(brightness_hists, axis=0)
        avg_depth = np.mean(depth_hists, axis=0) if depth_hists else None

        # Compute mean brightness across all captured frames for low-light detection.
        # Re-derive from the averaged histogram (weighted mean of bin centres).
        bin_centres = np.linspace(4.0, 252.0, 32)
        mean_brightness = float(np.dot(avg_brightness, bin_centres))

        return {"brightness": avg_brightness, "depth": avg_depth, "mean_brightness": mean_brightness}

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

    def _load_state(self) -> None:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text())
                self._room_name = data.get("name") or None
                b_sig = data.get("brightness_sig") or data.get("signature")  # backwards-compat
                if b_sig and isinstance(b_sig, list):
                    self._baseline_brightness = np.array(b_sig, dtype=float)
                d_sig = data.get("depth_sig")
                if d_sig and isinstance(d_sig, list):
                    self._baseline_depth = np.array(d_sig, dtype=float)
                log.info("RoomService: loaded state (room=%s)", self._room_name)
        except Exception as exc:
            log.warning("RoomService: could not load state: %s", exc)

    def _save_state(self) -> None:
        try:
            with self._lock:
                data: dict = {
                    "name": self._room_name,
                    "brightness_sig": (
                        self._baseline_brightness.tolist()
                        if self._baseline_brightness is not None
                        else None
                    ),
                    "depth_sig": (
                        self._baseline_depth.tolist()
                        if self._baseline_depth is not None
                        else None
                    ),
                }
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("RoomService: could not save state: %s", exc)


# ── Pure signal-processing helpers ────────────────────────────────────────────


def _compute_brightness_signature(frame: np.ndarray) -> np.ndarray:
    """
    Compute a normalised 32-bin brightness histogram as a visual room signature.

    The frame is aggressively downsampled before histogramming so that
    foreground objects (people, furniture) have little influence — only the
    overall luminance distribution of the scene is captured.
    """
    small = frame[::8, ::8]
    if small.ndim == 3:
        gray = small.mean(axis=2)
    else:
        gray = small.astype(float)
    hist, _ = np.histogram(gray.ravel(), bins=32, range=(0.0, 256.0))
    total = hist.sum()
    return hist.astype(float) / total if total > 0 else hist.astype(float)


def _compute_depth_signature(depth_arr: np.ndarray) -> np.ndarray:
    """
    Compute a normalised 16-bin depth histogram from a depth map (metres).

    NaN/Inf values and zeros (invalid pixels) are excluded.  The range is
    clipped to [0, _DEPTH_MAX_M] metres.
    """
    flat = depth_arr.ravel().astype(float)
    valid = flat[np.isfinite(flat) & (flat > 0)]
    if valid.size == 0:
        return np.zeros(_DEPTH_BINS, dtype=float)
    hist, _ = np.histogram(valid, bins=_DEPTH_BINS, range=(0.0, _DEPTH_MAX_M))
    total = hist.sum()
    return hist.astype(float) / total if total > 0 else hist.astype(float)


def _bhattacharyya(h1: np.ndarray, h2: np.ndarray) -> float:
    """Bhattacharyya coefficient: 1.0 = identical distributions, 0.0 = orthogonal."""
    return float(np.sum(np.sqrt(h1 * h2)))


def _compare_signatures(
    base_brightness: np.ndarray,
    base_depth: Optional[np.ndarray],
    curr_brightness: np.ndarray,
    curr_depth: Optional[np.ndarray],
) -> float:
    """
    Combined Bhattacharyya similarity between two room signatures.

    Returns a score in [0, 1] where 1.0 = identical.  If depth histograms
    are available for both baseline and current, depth is weighted at
    _W_DEPTH; otherwise only brightness is used.
    """
    b_score = _bhattacharyya(base_brightness, curr_brightness)
    if base_depth is not None and curr_depth is not None:
        d_score = _bhattacharyya(base_depth, curr_depth)
        return _W_BRIGHTNESS * b_score + _W_DEPTH * d_score
    return b_score


# Keep old name for backwards-compat with any external callers / tests that imported it.
_compute_signature = _compute_brightness_signature
