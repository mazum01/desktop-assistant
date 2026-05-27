"""
Room awareness service.

Tracks which room VERA is physically located in. Persists the room name
and a multi-angle visual signature across restarts. When sustained visual
divergence from the known baseline is detected — indicating VERA may have
been moved to a different room — VERA speaks a prompt asking the user to
confirm.

Signature methodology
---------------------
A room signature combines up to three signals:

1. **Gradient orientation embedding** (384-dim, L2-normalised) — *primary*:
   For each sweep angle a frame is downsampled to ~80×60 px and divided
   into a 6×8 grid of cells.  In each cell a magnitude-weighted gradient-
   orientation histogram (8 bins) is computed and normalised; the 48 cell
   histograms are concatenated and the full 384-dim vector is L2-normalised.
   Multiple sweep embeddings are mean-pooled and re-normalised.  Because the
   descriptor is gradient-based it is invariant to uniform brightness
   changes — turning the lights up or down will not shift the embedding,
   only structural changes (new walls, different furniture silhouettes) will.
   Comparison uses cosine similarity.

2. **Depth histogram** (16 bins over [0, _DEPTH_MAX_M] m, normalised) —
   *supplemental geometry signal*:
   The latest depth map from either the stereo or monocular depth service
   is cached via bus subscription.  When depth data is available it is
   blended into the similarity score (30 % weight), capturing wall
   distances and room geometry.

3. **Brightness histogram** (32 bins, normalised) — *low-light guard only*:
   Retained to compute ``mean_brightness`` for the low-light skip.  When
   scene illumination is below ``_LOW_LIGHT_THRESH`` the entire sample is
   discarded (neither incrementing nor resetting the divergence counter) so
   that turning off the lights cannot trigger a false room-change prompt.
   The brightness histogram is also used as a fallback comparison method
   if the gradient embedding is unavailable (e.g. legacy state files that
   predate this feature).

Similarity score:
    score = 0.70 × cosine(embedding) + 0.30 × B(depth)   [depth available]
    score = cosine(embedding)                              [no depth]
    score = 0.60 × B(brightness) + 0.40 × B(depth)        [legacy fallback]
    score = B(brightness)                                  [legacy, no depth]

Thresholds and timing are tunable via module-level constants.

Persistence
-----------
Room name and full signature are stored in ``config/room_state.json`` using
an atomic write (write to .json.tmp → replace) to survive unclean shutdowns.
The ``embedding`` key is new; old state files without it fall back to the
brightness-histogram comparison method automatically.

Topics subscribed:
    room.set              {"name": str}     — explicitly set the current room
    motion.position       {"angle": float}  — track current servo angle
    vision.depth_map      {…}               — cache latest stereo depth map
    vision.mono_depth_map {…}               — cache latest mono depth map
    perception.faces      {"faces": […]}    — cache latest face count; samples
                                             are skipped while faces are present

Topics published:
    room.updated   {"name": str}   — fired whenever the room name changes
    av.say         {"text": str}   — spoken prompts (unknown room, divergence)
    motion.pan_to  {"angle", …}    — servo pan during signature sweep
    tracking.set_face_tracking {"enabled": bool}  — paused during sweep

Configuration (config/assistant.yaml — room_detection section):
    sample_interval_s   float  interval between scene comparisons (default 600 s)
    consec_diverged     int    consecutive diverged samples before prompt (default 3)
    similarity_thresh   float  cosine similarity below this = "looks different" (default 0.85)
    prompt_cooldown_s   float  minimum seconds between prompts (default 1800 s)
    low_light_thresh    float  mean brightness below this skips the sample (default 15.0)
    skip_when_faces     bool   skip sample when faces are visible (default true)
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

# Default values — overridden by config/assistant.yaml room_detection section
_DEFAULT_SAMPLE_INTERVAL_S: float = 600.0   # check scene every 10 minutes
_DEFAULT_CONSEC_DIVERGED: int = 3           # 3 consecutive diverged samples ≈ 30 min sustained change
_DEFAULT_SIMILARITY_THRESH: float = 0.85    # combined score below this = "looks different"
_DEFAULT_PROMPT_COOLDOWN_S: float = 1800.0  # at most one room-change prompt every 30 minutes
_DEFAULT_LOW_LIGHT_THRESH: float = 15.0     # mean brightness below this → skip sample
_DEFAULT_SKIP_WHEN_FACES: bool = True       # skip sample while faces are visible

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

# Gradient embedding weights
_GRADIENT_CELLS_ROWS: int = 6       # rows of cells in the HOG grid
_GRADIENT_CELLS_COLS: int = 8       # columns of cells in the HOG grid
_GRADIENT_ORI_BINS: int = 8         # orientation bins per cell
# Embedding dims = _GRADIENT_CELLS_ROWS * _GRADIENT_CELLS_COLS * _GRADIENT_ORI_BINS = 384

_W_EMBEDDING: float = 0.70          # weight for embedding cosine sim when depth available
_W_EMBEDDING_DEPTH: float = 0.30    # weight for depth Bhattacharyya alongside embedding


class RoomService(Service):
    """
    Tracks which room VERA is in.

    On startup, speaks the current room name or asks "Which room am I in?"
    when no room has been assigned. Periodically captures a multi-angle
    panoramic signature and compares it to the stored baseline; if the
    signature looks consistently different over an extended period, VERA
    prompts the user to confirm whether the room has changed.

    Timing and sensitivity are configurable via the ``room_detection`` section
    of ``config/assistant.yaml``.
    """

    name = "room"
    tick_seconds = 0  # driven by internal sampling thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        state_path: Path = _STATE_PATH,
        cfg: Optional[dict] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._state_path = state_path

        # Runtime tunables (from config, with defaults)
        _cfg = cfg or {}
        self._sample_interval_s: float = float(
            _cfg.get("sample_interval_s", _DEFAULT_SAMPLE_INTERVAL_S)
        )
        self._consec_diverged_threshold: int = int(
            _cfg.get("consec_diverged", _DEFAULT_CONSEC_DIVERGED)
        )
        self._similarity_thresh: float = float(
            _cfg.get("similarity_thresh", _DEFAULT_SIMILARITY_THRESH)
        )
        self._prompt_cooldown_s: float = float(
            _cfg.get("prompt_cooldown_s", _DEFAULT_PROMPT_COOLDOWN_S)
        )
        self._low_light_thresh: float = float(
            _cfg.get("low_light_thresh", _DEFAULT_LOW_LIGHT_THRESH)
        )
        self._skip_when_faces: bool = bool(
            _cfg.get("skip_when_faces", _DEFAULT_SKIP_WHEN_FACES)
        )

        # Room state
        self._room_name: Optional[str] = None
        self._baseline_brightness: Optional[np.ndarray] = None  # 32-bin (low-light guard)
        self._baseline_depth: Optional[np.ndarray] = None        # 16-bin or None
        self._baseline_embedding: Optional[np.ndarray] = None    # 384-dim gradient embedding

        # Divergence tracking
        self._consec_diverged: int = 0
        self._last_prompt_ts: float = float("-inf")

        # Visualisation telemetry (written by sampler thread; read by get_status_dict)
        self._last_similarity: Optional[float] = None   # cosine/combined score from last run
        self._last_check_ts: float = float("-inf")      # monotonic timestamp of last _check_scene call
        self._last_skip_reason: Optional[str] = None    # "faces" | "low_light" | None

        # Current servo angle (updated via motion.position subscription)
        self._current_angle: Optional[float] = None

        # Latest face count cache (updated via perception.faces subscription).
        # When skip_when_faces=True, samples are skipped while faces are visible
        # to prevent person edges from polluting the room embedding.
        self._face_count: int = 0
        self._face_lock = threading.Lock()

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

    def get_status_dict(self) -> dict:
        """Return a snapshot of room detection state for the web dashboard."""
        now = time.monotonic()
        with self._lock:
            name = self._room_name
            baseline_ready = (
                self._baseline_embedding is not None
                or self._baseline_brightness is not None
            )
            last_similarity = self._last_similarity
            consec_diverged = self._consec_diverged
            last_prompt_ts = self._last_prompt_ts

        with self._face_lock:
            face_count = self._face_count

        last_check_ts = self._last_check_ts
        last_skip_reason = self._last_skip_reason

        last_check_age_s = (
            round(now - last_check_ts, 1) if last_check_ts != float("-inf") else None
        )
        last_prompt_age_s = (
            round(now - last_prompt_ts, 1) if last_prompt_ts != float("-inf") else None
        )

        return {
            "name": name,
            "baseline_ready": baseline_ready,
            "last_similarity": (
                round(float(last_similarity), 4) if last_similarity is not None else None
            ),
            "similarity_thresh": self._similarity_thresh,
            "consec_diverged": consec_diverged,
            "consec_diverged_threshold": self._consec_diverged_threshold,
            "last_check_age_s": last_check_age_s,
            "last_prompt_age_s": last_prompt_age_s,
            "last_skip_reason": last_skip_reason,
            "faces_present": face_count > 0,
            "skip_when_faces": self._skip_when_faces,
            "sample_interval_s": self._sample_interval_s,
        }


    def on_start(self) -> None:
        self._load_state()
        self.bus.subscribe("room.set",               self._on_set)
        self.bus.subscribe("motion.position",        self._on_motion_position)
        self.bus.subscribe("vision.depth_map",       self._on_depth_map)
        self.bus.subscribe("vision.mono_depth_map",  self._on_mono_depth_map)
        self.bus.subscribe("perception.faces",       self._on_faces)
        self._stop_evt.clear()
        self._timer_thread = threading.Thread(
            target=self._sample_loop, name="room-sampler", daemon=True
        )
        self._timer_thread.start()
        threading.Thread(
            target=self._announce_on_start, name="room-announce", daemon=True
        ).start()
        log.info(
            "RoomService started (room=%s, interval=%.0fs, skip_faces=%s)",
            self._room_name or "unknown",
            self._sample_interval_s,
            self._skip_when_faces,
        )

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

    def _on_faces(self, _topic, payload) -> None:
        """Cache the number of faces currently visible."""
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        with self._face_lock:
            self._face_count = len(faces) if isinstance(faces, list) else 0

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
        """Speak the room name on startup — but verify first.

        Waits for the AV service and cameras to settle, then takes a quick
        scene sample.  If the current scene matches the stored baseline with
        sufficient confidence the room name is announced normally.  If it
        doesn't match (VERA may have been moved while powered off) a tentative
        message is spoken and the regular divergence detection takes over.
        """
        time.sleep(6.0)  # allow AV service and cameras to initialise
        if not self.bus:
            return

        with self._lock:
            room = self._room_name
            has_baseline = (
                self._baseline_embedding is not None
                or self._baseline_brightness is not None
            )

        if not room:
            self.bus.publish(
                "av.say",
                {"text": "Which room am I in? You can tell me with 'vera room set <name>'."},
            )
            return

        # If there is no baseline we can't verify — announce normally.
        if not has_baseline:
            self.bus.publish("av.say", {"text": f"I'm in the {room}."})
            return

        # Grab a quick single-frame signature (no servo sweep needed here).
        sig = self._panoramic_signature()
        if sig is None:
            # Camera not ready — fall back to trusting the saved name.
            self.bus.publish("av.say", {"text": f"I'm in the {room}."})
            return

        with self._lock:
            similarity = _compare_signatures(
                self._baseline_brightness,
                self._baseline_depth,
                sig["brightness"],
                sig["depth"],
                self._baseline_embedding,
                sig.get("embedding"),
            )
            self._last_similarity = similarity
            thresh = self._similarity_thresh

        log.info(
            "RoomService: boot scene check — similarity=%.3f threshold=%.2f room=%s",
            similarity, thresh, room,
        )

        if similarity >= thresh:
            self.bus.publish("av.say", {"text": f"I'm in the {room}."})
        else:
            # Looks like VERA may have been moved — be honest about it.
            self.bus.publish(
                "av.say",
                {
                    "text": (
                        f"I was last in the {room}, but things look different. "
                        "I'll figure out where I am."
                    )
                },
            )
            log.info(
                "RoomService: boot mismatch (sim=%.3f) — suppressing confident room announce",
                similarity,
            )

    def _sample_loop(self) -> None:
        """Background thread: sample scene every sample_interval_s seconds.

        Runs an initial check after a short warmup delay so the stability
        gauge is populated on the web GUI shortly after startup rather than
        waiting the full sample interval.
        """
        # Initial warmup check: wait 8s for cameras to stabilise, then check once.
        if not self._stop_evt.wait(timeout=8.0):
            self._check_scene()
        # Regular interval loop.
        while not self._stop_evt.wait(timeout=self._sample_interval_s):
            self._check_scene()

    def _check_scene(self) -> None:
        """Capture panoramic signature; compare with baseline; prompt if diverged long enough."""
        self._last_check_ts = time.monotonic()

        # Face-skip: if faces are visible, the embedding would include person
        # edges that aren't part of the room structure — defer judgment.
        if self._skip_when_faces:
            with self._face_lock:
                face_count = self._face_count
            if face_count > 0:
                self._last_skip_reason = "faces"
                log.debug(
                    "RoomService: %d face(s) visible — skipping sample to avoid person-edge pollution",
                    face_count,
                )
                return

        sig = self._panoramic_signature()
        if sig is None:
            return

        # Low-light skip: if the scene is too dark to be meaningful, defer
        # judgment without touching the divergence counter.
        if sig.get("mean_brightness", 255.0) < self._low_light_thresh:
            self._last_skip_reason = "low_light"
            log.debug(
                "RoomService: scene too dark (mean=%.1f) — skipping sample",
                sig["mean_brightness"],
            )
            return

        self._last_skip_reason = None

        should_prompt = False
        should_save = False
        room_name: Optional[str] = None

        with self._lock:
            if self._baseline_brightness is None:
                # Establish baseline on first successful capture
                self._baseline_brightness = sig["brightness"].copy()
                self._baseline_depth = sig["depth"].copy() if sig["depth"] is not None else None
                self._baseline_embedding = (
                    sig["embedding"].copy() if sig.get("embedding") is not None else None
                )
                should_save = True
                log.debug("RoomService: established initial visual baseline")
            else:
                similarity = _compare_signatures(
                    self._baseline_brightness,
                    self._baseline_depth,
                    sig["brightness"],
                    sig["depth"],
                    self._baseline_embedding,
                    sig.get("embedding"),
                )
                self._last_similarity = similarity
                log.debug(
                    "RoomService: scene similarity=%.3f (threshold=%.2f)",
                    similarity,
                    self._similarity_thresh,
                )

                if similarity < self._similarity_thresh:
                    self._consec_diverged += 1
                    log.info(
                        "RoomService: scene diverged (%d/%d)",
                        self._consec_diverged,
                        self._consec_diverged_threshold,
                    )
                else:
                    self._consec_diverged = 0

                elapsed_since_prompt = time.monotonic() - self._last_prompt_ts
                if (
                    self._consec_diverged >= self._consec_diverged_threshold
                    and elapsed_since_prompt >= self._prompt_cooldown_s
                ):
                    should_prompt = True
                    room_name = self._room_name
                    self._last_prompt_ts = time.monotonic()

        if should_save:
            self._save_state()
            return

        # Persist the updated similarity score so web GUI shows it immediately on next restart.
        self._save_state()

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
            self._baseline_embedding = (
                sig["embedding"].copy() if sig.get("embedding") is not None else None
            )
        self._save_state()
        log.info("RoomService: new visual baseline captured after room set")

    def _panoramic_signature(self) -> Optional[dict]:
        """
        Sweep the servo through _SWEEP_ANGLES, capture a frame at each position,
        and return the averaged brightness histogram, depth histogram, and
        gradient orientation embedding.

        The gradient embedding is the primary comparison signal: it is computed
        per frame, mean-pooled across sweep angles, and re-normalised to unit
        length.  The brightness histogram is retained for the low-light guard
        and as a legacy fallback only.

        If no bus/servo is available, falls back to a single-frame capture from
        the current position.  Face tracking is paused for the duration of the
        sweep to prevent servo contention.
        """
        if self._vision_svc is None:
            return None

        original_angle = self._current_angle
        sweep_possible = (self.bus is not None) and (original_angle is not None)

        brightness_hists: list[np.ndarray] = []
        embedding_vecs: list[np.ndarray] = []
        depth_hists: list[np.ndarray] = []

        def _grab_one() -> None:
            try:
                frame = self._vision_svc.latest_frame()
                if frame is not None:
                    brightness_hists.append(_compute_brightness_signature(frame))
                    embedding_vecs.append(_compute_gradient_embedding(frame))
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

        # Mean-pool gradient embeddings across sweep angles, then re-normalise
        # to unit length (mean of unit vectors approximates the dominant
        # orientation distribution of the panoramic view).
        avg_embedding: Optional[np.ndarray] = None
        if embedding_vecs:
            pooled = np.mean(embedding_vecs, axis=0)
            norm = np.linalg.norm(pooled)
            avg_embedding = pooled / (norm + 1e-8)

        # Compute mean brightness from averaged histogram for low-light guard.
        bin_centres = np.linspace(4.0, 252.0, 32)
        mean_brightness = float(np.dot(avg_brightness, bin_centres))

        return {
            "brightness": avg_brightness,
            "depth": avg_depth,
            "embedding": avg_embedding,
            "mean_brightness": mean_brightness,
        }

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
                e_sig = data.get("embedding")
                if e_sig and isinstance(e_sig, list):
                    self._baseline_embedding = np.array(e_sig, dtype=float)
                # Restore last known similarity so the web GUI shows a value
                # immediately on startup instead of "—" until the next check.
                saved_sim = data.get("last_similarity")
                if saved_sim is not None:
                    try:
                        self._last_similarity = float(saved_sim)
                    except (TypeError, ValueError):
                        pass
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
                    "embedding": (
                        self._baseline_embedding.tolist()
                        if self._baseline_embedding is not None
                        else None
                    ),
                    "last_similarity": self._last_similarity,
                }
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._state_path)
        except Exception as exc:
            log.warning("RoomService: could not save state: %s", exc)


# ── Pure signal-processing helpers ────────────────────────────────────────────


def _compute_gradient_embedding(frame: np.ndarray) -> np.ndarray:
    """
    HOG-style gradient orientation embedding from a camera frame.

    The frame is downsampled to ~80×60 px, Sobel gradients are computed, and
    the result is divided into a grid of _GRADIENT_CELLS_ROWS × _GRADIENT_CELLS_COLS
    cells.  Within each cell a magnitude-weighted orientation histogram
    (_GRADIENT_ORI_BINS bins) is accumulated and normalised.  The full
    (_GRADIENT_CELLS_ROWS × _GRADIENT_CELLS_COLS × _GRADIENT_ORI_BINS)-dim
    vector is L2-normalised before return.

    **Lighting invariance**: because the descriptor is built from *gradients*
    (not raw pixel values), uniform brightness changes — turning lights up or
    down — have no effect on the embedding.  Only structural changes (new
    walls, different furniture silhouettes) shift the descriptor.
    """
    small = frame[::8, ::8]
    if small.ndim == 3:
        gray = small.mean(axis=2).astype(float)
    else:
        gray = small.astype(float)

    # Sobel-like gradients using simple finite differences (no scipy needed).
    gy = gray[2:, 1:-1] - gray[:-2, 1:-1]   # vertical
    gx = gray[1:-1, 2:] - gray[1:-1, :-2]   # horizontal
    magnitude = np.sqrt(gx**2 + gy**2)
    orientation = np.arctan2(gy, gx)          # in [-π, π]

    H, W = magnitude.shape
    cell_h = H // _GRADIENT_CELLS_ROWS
    cell_w = W // _GRADIENT_CELLS_COLS

    features: list[np.ndarray] = []
    for r in range(_GRADIENT_CELLS_ROWS):
        for c in range(_GRADIENT_CELLS_COLS):
            r0, r1 = r * cell_h, (r + 1) * cell_h
            c0, c1 = c * cell_w, (c + 1) * cell_w
            cell_mag = magnitude[r0:r1, c0:c1].ravel()
            cell_ori = orientation[r0:r1, c0:c1].ravel()
            hist, _ = np.histogram(
                cell_ori, bins=_GRADIENT_ORI_BINS,
                range=(-np.pi, np.pi), weights=cell_mag,
            )
            total = hist.sum()
            features.append(hist / (total + 1e-8))

    embedding = np.concatenate(features)
    norm = np.linalg.norm(embedding)
    return embedding / (norm + 1e-8)


def _cosine_similarity(e1: np.ndarray, e2: np.ndarray) -> float:
    """Cosine similarity between two pre-normalised L2 vectors. Range [−1, 1]."""
    return float(np.dot(e1, e2))



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
    base_embedding: Optional[np.ndarray] = None,
    curr_embedding: Optional[np.ndarray] = None,
) -> float:
    """
    Combined similarity score between two room signatures.  Returns a value in
    [0, 1] where 1.0 = identical.

    When gradient embeddings are available for both baseline and current, the
    primary comparison is cosine similarity (lighting-invariant).  If depth
    histograms are also available they contribute at _W_EMBEDDING_DEPTH weight:
        score = _W_EMBEDDING × cosine + _W_EMBEDDING_DEPTH × B(depth)

    Falls back to the Bhattacharyya brightness + depth comparison when
    embeddings are not available (e.g. legacy state files):
        score = _W_BRIGHTNESS × B(brightness) + _W_DEPTH × B(depth)
    """
    if base_embedding is not None and curr_embedding is not None:
        sim = _cosine_similarity(base_embedding, curr_embedding)
        if base_depth is not None and curr_depth is not None:
            d_score = _bhattacharyya(base_depth, curr_depth)
            return _W_EMBEDDING * sim + _W_EMBEDDING_DEPTH * d_score
        return sim

    # Legacy fallback: brightness histogram Bhattacharyya
    b_score = _bhattacharyya(base_brightness, curr_brightness)
    if base_depth is not None and curr_depth is not None:
        d_score = _bhattacharyya(base_depth, curr_depth)
        return _W_BRIGHTNESS * b_score + _W_DEPTH * d_score
    return b_score


# Keep old name for backwards-compat with any external callers / tests that imported it.
_compute_signature = _compute_brightness_signature
