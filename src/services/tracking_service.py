"""
Head tracking service.

Subscribes to ``perception.faces``, selects the primary face (highest
confidence), feeds it to ``HeadTracker``, and publishes ``motion.pan_to``
at 20 Hz so the servo follows with natural spring-damper motion.

When no face is visible for more than *idle_timeout_s* the tracker enters
idle mode and produces slow, human-like gaze drifts.

**Person seek** — when object detection is enabled and a ``person`` bounding
box appears on ``perception.objects`` but no face has been acquired yet, the
tracker uses the person's horizontal centre as a soft seek target.  This lets
the head pan toward the person so SCRFD can pick up their face.  Once a face
locks on, face tracking takes over immediately; person seek is inactive while
a face is tracked.

**Speaking motion** — while DA is talking (``av.speaking_started`` …
``av.spoke``) the head nods with a gentle sinusoidal offset so the robot
looks more alive.  Amplitude and frequency are config-tunable under
``head_tracking.speaking_motion_*``.

Topics subscribed
-----------------
perception.faces    ``{faces: [{centroid, confidence, …}], …}``
perception.objects  ``{objects: [{label, bbox:[x1,y1,x2,y2], …}], frame_w, …}``
motion.position     ``{angle: float}``  — servo position feedback
tracking.set_face_tracking   ``{"enabled": bool}``
tracking.set_random_motion   ``{"enabled": bool}``
tracking.set_person_seek     ``{"enabled": bool}``
av.speaking_started ``{"text": str, "ts": float}``
av.spoke            ``{"text": str, "ts": float}``

Topics published
----------------
motion.pan_to       ``{angle: float, move_time_ms: float}``
tracking.face_tracking_changed   ``{"enabled": bool}``
tracking.random_motion_changed   ``{"enabled": bool}``
tracking.person_seek_changed     ``{"enabled": bool}``
"""

from __future__ import annotations

import logging
import math
import statistics
import threading
import time
from pathlib import Path
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.motion.head_tracker import HeadTracker, HeadTrackerConfig, TUNABLE_FIELDS, PRESETS

log = logging.getLogger(__name__)

_UPDATE_HZ = 20
_UPDATE_INTERVAL = 1.0 / _UPDATE_HZ

# Live-tuning debug telemetry rate (~10 Hz)
_DEBUG_PUBLISH_HZ = 10
_DEBUG_PUBLISH_INTERVAL = 1.0 / _DEBUG_PUBLISH_HZ

# Maximum age (seconds) of a person detection hint before it is discarded.
# At 2 fps object detection this equals ~4 missed frames.
_PERSON_SEEK_STALE_S = 2.0

# Path to assistant.yaml — used by save_params to persist tuned values.
_CONFIG_YAML_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "assistant.yaml"
)


class TrackingService(Service):
    """20 Hz head-tracking loop driven by face detection events."""

    name = "tracking"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[HeadTrackerConfig] = None,
        enabled: bool = True,
        face_tracking_enabled: bool = True,
        random_motion_enabled: bool = True,
        person_seek_enabled: bool = True,
        speaking_motion_enabled: bool = True,
        speaking_motion_amplitude_deg: float = 1.5,
        speaking_motion_freq_hz: float = 2.5,
    ) -> None:
        super().__init__(bus=bus)
        self._tracker_cfg = config or HeadTrackerConfig()
        self._enabled = enabled
        self._face_tracking_enabled = face_tracking_enabled
        self._random_motion_enabled = random_motion_enabled
        self._person_seek_enabled = person_seek_enabled
        # Speaking motion: gentle sinusoidal nod while DA is talking
        self._speaking_motion_enabled = speaking_motion_enabled
        self._speaking_motion_amplitude = speaking_motion_amplitude_deg
        self._speaking_motion_freq = speaking_motion_freq_hz
        self._speaking_until: float = 0.0  # monotonic timestamp; >now means DA is speaking
        self._tracker: Optional[HeadTracker] = None
        self._current_face_cx: Optional[float] = None
        self._current_servo_angle: Optional[float] = None
        # Person-seek state: pixel x-centre from latest "person" detection
        self._person_cx: Optional[float] = None
        self._person_cx_ts: float = 0.0
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubs: list = []

        # ── Live tuning / auto-tune state ─────────────────────────────
        self._last_debug_publish_t: float = 0.0
        # Auto-tune state machine: None when idle; otherwise a dict carrying
        #   stage:    'noise' | 'response' | 'done' | 'aborted'
        #   end_t:    monotonic deadline for current stage
        #   samples:  list of (face_x_raw, servo_angle, t_since_start)
        #   probe_gains: remaining tracking_gain values to probe (response stage)
        #   results:  list of (gain, lag_s, overshoot_deg) per probe
        self._autotune: Optional[dict] = None
        self._autotune_lock = threading.Lock()

    @property
    def face_tracking_enabled(self) -> bool:
        return self._face_tracking_enabled

    @property
    def random_motion_enabled(self) -> bool:
        return self._random_motion_enabled

    @property
    def person_seek_enabled(self) -> bool:
        return self._person_seek_enabled

    def update_frame_width(self, frame_width: int) -> None:
        """Update the horizontal pixel width used for tracking math.

        Called when camera rotation changes so the tracker re-centres
        correctly without requiring a daemon restart.
        """
        with self._lock:
            self._tracker_cfg.frame_width = frame_width
            if self._tracker is not None:
                self._tracker._cfg.frame_width = frame_width
        log.info("TrackingService: frame_width updated to %d", frame_width)

    # ── Public accessors for live-tuning REST endpoints ──────────────────

    def get_tunable_params(self) -> dict:
        """Snapshot of current tunable params + ranges + preset names."""
        params = self._tracker.get_config() if self._tracker is not None else {}
        return {
            "params": params,
            "ranges": {k: list(v) for k, v in TUNABLE_FIELDS.items()},
            "presets": list(PRESETS.keys()),
        }

    def set_tunable_param(self, name: str, value: float) -> bool:
        if self._tracker is None:
            return False
        return self._tracker.update_config(name, float(value))

    def autotune_status(self) -> dict:
        with self._autotune_lock:
            if self._autotune is None:
                return {"active": False, "stage": "idle"}
            now = time.monotonic()
            at = self._autotune
            stage = at["stage"]
            if stage == "noise":
                t_rem = max(0.0, at["end_t"] - now)
            elif stage == "response":
                probes_left = len(at["probe_gains"]) - at["probe_idx"]
                t_rem = max(0.0, at["probe_end_t"] - now) + 4.0 * max(0, probes_left - 1)
            else:
                t_rem = 0.0
            return {"active": stage not in ("done", "aborted"),
                    "stage": stage, "t_remaining": t_rem}

    def on_start(self) -> None:
        if not self._enabled:
            log.info("TrackingService disabled via config")
            return

        self._tracker = HeadTracker(
            initial_servo_angle=180.0,
            config=self._tracker_cfg,
        )

        self._unsubs.append(self.bus.subscribe("perception.faces", self._on_faces))
        self._unsubs.append(self.bus.subscribe("perception.objects", self._on_objects))
        self._unsubs.append(self.bus.subscribe("motion.position", self._on_position))
        self._unsubs.append(self.bus.subscribe("tracking.set_face_tracking", self._on_set_face_tracking))
        self._unsubs.append(self.bus.subscribe("tracking.set_random_motion", self._on_set_random_motion))
        self._unsubs.append(self.bus.subscribe("tracking.set_person_seek", self._on_set_person_seek))
        self._unsubs.append(self.bus.subscribe("av.speaking_started", self._on_speaking_started))
        self._unsubs.append(self.bus.subscribe("av.spoke", self._on_spoke))
        # Live tuning / auto-tune
        self._unsubs.append(self.bus.subscribe("tracking.set_param", self._on_set_param))
        self._unsubs.append(self.bus.subscribe("tracking.get_params", self._on_get_params))
        self._unsubs.append(self.bus.subscribe("tracking.save_params", self._on_save_params))
        self._unsubs.append(self.bus.subscribe("tracking.reset_params", self._on_reset_params))
        self._unsubs.append(self.bus.subscribe("tracking.apply_preset", self._on_apply_preset))
        self._unsubs.append(self.bus.subscribe("tracking.start_autotune", self._on_start_autotune))
        self._unsubs.append(self.bus.subscribe("tracking.cancel_autotune", self._on_cancel_autotune))

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="head-tracker",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "TrackingService started (%d Hz, enabled=%s, face_tracking=%s, random_motion=%s, person_seek=%s)",
            _UPDATE_HZ, self._enabled, self._face_tracking_enabled,
            self._random_motion_enabled, self._person_seek_enabled,
        )

    def on_stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        log.info("TrackingService stopped")

    # ── Bus handlers ─────────────────────────────────────────────────────

    def _on_set_face_tracking(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "enabled" not in payload:
            return
        new_state = bool(payload["enabled"])
        if new_state == self._face_tracking_enabled:
            return
        self._face_tracking_enabled = new_state
        log.info("TrackingService: face tracking %s", "enabled" if new_state else "disabled")
        if not new_state:
            with self._lock:
                self._current_face_cx = None
        self.bus.publish("tracking.face_tracking_changed", {"enabled": new_state})

    def _on_set_random_motion(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "enabled" not in payload:
            return
        new_state = bool(payload["enabled"])
        if new_state == self._random_motion_enabled:
            return
        self._random_motion_enabled = new_state
        log.info("TrackingService: random motion %s", "enabled" if new_state else "disabled")
        self.bus.publish("tracking.random_motion_changed", {"enabled": new_state})

    def _on_set_person_seek(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "enabled" not in payload:
            return
        new_state = bool(payload["enabled"])
        if new_state == self._person_seek_enabled:
            return
        self._person_seek_enabled = new_state
        log.info("TrackingService: person seek %s", "enabled" if new_state else "disabled")
        if not new_state:
            with self._lock:
                self._person_cx = None
                self._person_cx_ts = 0.0
        self.bus.publish("tracking.person_seek_changed", {"enabled": new_state})

    def _on_faces(self, _topic, payload) -> None:
        if not self._face_tracking_enabled:
            with self._lock:
                self._current_face_cx = None
            return
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        if faces:
            primary = max(faces, key=lambda f: f.get("confidence", 0.0))
            cx = primary.get("centroid", [None, None])[0]
            with self._lock:
                self._current_face_cx = float(cx) if cx is not None else None
        else:
            with self._lock:
                self._current_face_cx = None

    def _on_objects(self, _topic, payload) -> None:
        """Update person-seek hint from object detection."""
        if not self._person_seek_enabled or not self._face_tracking_enabled:
            return
        if not isinstance(payload, dict):
            return
        objects = payload.get("objects") or []
        frame_w = payload.get("frame_w", 0)
        if not frame_w:
            return
        # Find the highest-confidence "person" detection.
        persons = [o for o in objects if o.get("label") == "person"]
        if not persons:
            with self._lock:
                self._person_cx = None
                self._person_cx_ts = 0.0
            return
        best = max(persons, key=lambda o: o.get("confidence", 0.0))
        bbox = best.get("bbox")
        if not bbox or len(bbox) < 4:
            return
        cx = (bbox[0] + bbox[2]) / 2.0
        with self._lock:
            self._person_cx = cx
            self._person_cx_ts = time.monotonic()

    def _on_position(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._lock:
                self._current_servo_angle = float(payload["angle"])

    def _on_speaking_started(self, _topic, _payload) -> None:
        """Mark the head as 'speaking' until av.spoke arrives (max 60 s safety cap)."""
        self._speaking_until = time.monotonic() + 60.0

    def _on_spoke(self, _topic, _payload) -> None:
        """Clear the speaking window as soon as TTS finishes."""
        self._speaking_until = 0.0

    # ── Live tuning bus handlers ─────────────────────────────────────────

    def _on_set_param(self, _topic, payload) -> None:
        """Apply a live HeadTrackerConfig field change without restart."""
        if not isinstance(payload, dict):
            return
        name = payload.get("name")
        value = payload.get("value")
        if not isinstance(name, str) or value is None or self._tracker is None:
            return
        try:
            ok = self._tracker.update_config(name, float(value))
        except (TypeError, ValueError):
            ok = False
        self.bus.publish("tracking.param_changed", {
            "name": name, "value": float(value) if ok else None, "ok": ok,
        })
        if ok:
            log.info("TrackingService: %s = %.4f", name, float(value))

    def _on_get_params(self, _topic, _payload) -> None:
        if self._tracker is None:
            return
        params = self._tracker.get_config()
        # Include ranges so the UI can render sliders without hard-coding limits.
        ranges = {k: list(v) for k, v in TUNABLE_FIELDS.items()}
        self.bus.publish("tracking.params", {
            "params": params, "ranges": ranges, "presets": list(PRESETS.keys()),
        })

    def _on_save_params(self, _topic, _payload) -> None:
        if self._tracker is None:
            return
        params = self._tracker.get_config()
        ok = _persist_head_tracking_params(_CONFIG_YAML_PATH, params)
        self.bus.publish("tracking.save_params_done", {
            "ok": ok, "path": str(_CONFIG_YAML_PATH),
        })

    def _on_reset_params(self, _topic, _payload) -> None:
        """Restore the baked-in 'default' preset values."""
        self._on_apply_preset("tracking.apply_preset", {"name": "default"})

    def _on_apply_preset(self, _topic, payload) -> None:
        if self._tracker is None:
            return
        name = (payload or {}).get("name") if isinstance(payload, dict) else None
        preset = PRESETS.get(name or "")
        if not preset:
            self.bus.publish("tracking.preset_applied", {"name": name, "ok": False})
            return
        for k, v in preset.items():
            self._tracker.update_config(k, float(v))
        self.bus.publish("tracking.preset_applied", {
            "name": name, "ok": True, "params": self._tracker.get_config(),
        })
        log.info("TrackingService: applied preset '%s'", name)

    # ── Auto-tune ────────────────────────────────────────────────────────

    def _on_start_autotune(self, _topic, _payload) -> None:
        with self._autotune_lock:
            if self._autotune is not None and self._autotune.get("stage") not in ("done", "aborted"):
                return  # already running
            self._autotune = {
                "stage": "noise",
                "start_t": time.monotonic(),
                "end_t":   time.monotonic() + 5.0,
                "samples": [],
                "probe_gains": [0.20, 0.30, 0.45, 0.60],
                "probe_idx": 0,
                "probe_end_t": 0.0,
                "results": [],
            }
        log.info("TrackingService: auto-tune started")
        self.bus.publish("tracking.autotune_progress", {
            "stage": "noise", "t_remaining": 5.0,
            "msg": "Stay still and look at the camera (5 s)…",
        })

    def _on_cancel_autotune(self, _topic, _payload) -> None:
        with self._autotune_lock:
            if self._autotune is not None:
                self._autotune["stage"] = "aborted"
        self.bus.publish("tracking.autotune_progress", {
            "stage": "aborted", "t_remaining": 0.0, "msg": "Cancelled.",
        })

    def _autotune_tick(self, face_cx: Optional[float], servo_angle: float) -> None:
        """Drive the auto-tune state machine; called once per loop iteration."""
        with self._autotune_lock:
            at = self._autotune
            if at is None:
                return
            stage = at["stage"]
            if stage in ("done", "aborted"):
                return
            now = time.monotonic()

            if stage == "noise":
                if face_cx is not None:
                    at["samples"].append(face_cx)
                t_remain = max(0.0, at["end_t"] - now)
                if now >= at["end_t"]:
                    samples = at["samples"]
                    if len(samples) < 10:
                        self._autotune = None
                        self.bus.publish("tracking.autotune_progress", {
                            "stage": "aborted", "t_remaining": 0.0,
                            "msg": "No face detected — aborted.",
                        })
                        return
                    sigma = statistics.pstdev(samples) if len(samples) > 1 else 15.0
                    new_r = max(100.0, min(1500.0, (3.0 * sigma) ** 2))
                    if self._tracker is not None:
                        self._tracker.update_config("kalman_r", new_r)
                    at["noise_sigma"] = sigma
                    at["kalman_r"] = new_r
                    # Advance to response stage
                    at["stage"] = "response"
                    at["probe_idx"] = 0
                    at["samples"] = []
                    at["probe_end_t"] = now + 4.0
                    if self._tracker is not None:
                        self._tracker.update_config("tracking_gain", at["probe_gains"][0])
                    self.bus.publish("tracking.autotune_progress", {
                        "stage": "response", "t_remaining": 16.0,
                        "msg": f"Detected noise σ={sigma:.1f}px. Now slowly wave your head left-right…",
                    })
                else:
                    self.bus.publish("tracking.autotune_progress", {
                        "stage": "noise", "t_remaining": t_remain,
                        "msg": f"Stay still ({t_remain:.1f}s)…",
                    })

            elif stage == "response":
                # Each probe: 4 s at a given gain, collect (face_x, servo_angle) every tick.
                if face_cx is not None:
                    at["samples"].append((face_cx, servo_angle, now))
                if now >= at["probe_end_t"]:
                    samples = at["samples"]
                    gain = at["probe_gains"][at["probe_idx"]]
                    if len(samples) >= 20:
                        lag, overshoot = _analyse_response(samples)
                        at["results"].append({
                            "gain": gain, "lag_s": lag, "overshoot_deg": overshoot,
                        })
                    at["probe_idx"] += 1
                    at["samples"] = []
                    if at["probe_idx"] < len(at["probe_gains"]):
                        next_gain = at["probe_gains"][at["probe_idx"]]
                        if self._tracker is not None:
                            self._tracker.update_config("tracking_gain", next_gain)
                        at["probe_end_t"] = now + 4.0
                        self.bus.publish("tracking.autotune_progress", {
                            "stage": "response",
                            "t_remaining": 4.0 * (len(at["probe_gains"]) - at["probe_idx"]),
                            "msg": f"Probing gain={next_gain:.2f}…",
                        })
                    else:
                        # Pick the best gain (minimise lag + 0.3·overshoot)
                        results = at["results"]
                        if not results:
                            self._autotune = None
                            self.bus.publish("tracking.autotune_progress", {
                                "stage": "aborted", "t_remaining": 0.0,
                                "msg": "Auto-tune failed — no face detected during response stage.",
                            })
                            return
                        scored = [(r["lag_s"] + 0.3 * r["overshoot_deg"], r) for r in results]
                        scored.sort(key=lambda x: x[0])
                        best = scored[0][1]
                        if self._tracker is not None:
                            self._tracker.update_config("tracking_gain", best["gain"])
                        at["stage"] = "done"
                        log.info(
                            "TrackingService: auto-tune done — kalman_r=%.0f gain=%.2f lag=%.3fs overshoot=%.2f°",
                            at.get("kalman_r", 0.0), best["gain"],
                            best["lag_s"], best["overshoot_deg"],
                        )
                        self.bus.publish("tracking.autotune_done", {
                            "kalman_r": at.get("kalman_r"),
                            "tracking_gain": best["gain"],
                            "lag_s": best["lag_s"],
                            "overshoot_deg": best["overshoot_deg"],
                            "all_results": results,
                            "params": self._tracker.get_config() if self._tracker else {},
                        })

    def _maybe_publish_debug(self, now: float) -> None:
        if self._tracker is None:
            return
        if now - self._last_debug_publish_t < _DEBUG_PUBLISH_INTERVAL:
            return
        self._last_debug_publish_t = now
        state = self._tracker.get_debug_state()
        state["t"] = now
        self.bus.publish("tracking.debug", state)

    # ── Tracking loop ────────────────────────────────────────────────────

    def _loop(self) -> None:
        last_t = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            dt = now - last_t
            last_t = now

            with self._lock:
                # When face tracking is disabled, pass None so the tracker
                # goes to idle mode. When random motion is also disabled,
                # skip publishing pan_to entirely so the head stays put.
                face_cx = self._current_face_cx if self._face_tracking_enabled else None
                servo_angle = self._current_servo_angle
                # Person-seek: use person centroid as a soft target only
                # when no face is currently locked and the hint is fresh.
                person_cx = None
                if (
                    face_cx is None
                    and self._person_seek_enabled
                    and self._person_cx is not None
                    and (now - self._person_cx_ts) < _PERSON_SEEK_STALE_S
                ):
                    person_cx = self._person_cx

            if self._tracker is None:
                time.sleep(_UPDATE_INTERVAL)
                continue

            # If random motion is disabled and there's no face or person to track, hold position.
            if not self._random_motion_enabled and face_cx is None and person_cx is None:
                time.sleep(_UPDATE_INTERVAL)
                continue

            # Person-seek supplies the centroid when no face is locked.
            effective_cx = face_cx if face_cx is not None else person_cx

            target = self._tracker.update(
                face_cx=effective_cx,
                dt=dt,
                current_servo_angle=servo_angle,
            )

            # Speaking motion: add a sinusoidal nod while DA is talking.
            if self._speaking_motion_enabled and now < self._speaking_until:
                nod = self._speaking_motion_amplitude * math.sin(
                    2.0 * math.pi * self._speaking_motion_freq * now
                )
                target = target + nod

            move_ms = _UPDATE_INTERVAL * 1000.0 * 2.0
            self.bus.publish("motion.pan_to", {
                "angle": round(target, 2),
                "move_time_ms": round(move_ms, 1),
            })

            # Live telemetry + auto-tune
            self._autotune_tick(effective_cx, target)
            self._maybe_publish_debug(now)

            elapsed = time.monotonic() - now
            sleep_t = max(0.001, _UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_t)


# ── Module-level helpers ──────────────────────────────────────────────────────

def _analyse_response(samples: list[tuple[float, float, float]]) -> tuple[float, float]:
    """Compute (lag_s, overshoot_deg) from a response-stage sample window.

    samples: list of (face_x_raw_px, servo_target_deg, monotonic_t_s)

    Method:
      * Centre both signals about their means.
      * Compute discrete cross-correlation; the peak lag is taken as the
        servo's tracking lag.
      * Overshoot is the residual angular swing of the servo after the face
        signal has reversed direction — approximated as the difference between
        the servo's peak amplitude and what the gain*FOV would predict.
    """
    if len(samples) < 20:
        return 0.5, 0.0

    n = len(samples)
    times = [s[2] for s in samples]
    dt = (times[-1] - times[0]) / max(1, n - 1)
    if dt <= 0:
        return 0.5, 0.0

    face = [s[0] for s in samples]
    servo = [s[1] for s in samples]
    face_mean = sum(face) / n
    servo_mean = sum(servo) / n
    f = [v - face_mean for v in face]
    s = [v - servo_mean for v in servo]

    # Cross-correlation: find shift k (0..max_lag) that maximises Σ f[i] * s[i+k].
    max_lag = min(int(0.6 / dt), n // 2)
    best_k, best_score = 0, -1e30
    for k in range(0, max_lag + 1):
        score = sum(f[i] * s[i + k] for i in range(n - k))
        if score > best_score:
            best_score, best_k = score, k
    lag_s = best_k * dt

    # Overshoot: peak-to-trough swing on the servo vs face (degrees).
    face_swing = (max(face) - min(face))
    servo_swing = (max(servo) - min(servo))
    # Roughly: 1280 px ↔ 100° (default FOV) → 12.8 px / deg. Overshoot is the
    # extra servo travel beyond what a perfectly matched response would produce.
    expected_servo_swing = face_swing / 12.8
    overshoot = max(0.0, servo_swing - expected_servo_swing)

    return float(lag_s), float(overshoot)


def _persist_head_tracking_params(path: Path, params: dict) -> bool:
    """Persist the tunable head_tracking params into config/assistant.yaml.

    Tries ruamel.yaml first (preserves comments and key order). Falls back
    to PyYAML round-trip (loses comments) if ruamel isn't available.
    """
    try:
        # Prefer ruamel.yaml so comments are preserved
        from ruamel.yaml import YAML

        yaml_rt = YAML()
        yaml_rt.preserve_quotes = True
        with open(path, "r") as fh:
            data = yaml_rt.load(fh)
        if not isinstance(data, dict):
            return False
        ht = data.get("head_tracking")
        if not isinstance(ht, dict):
            ht = {}
            data["head_tracking"] = ht
        for k, v in params.items():
            ht[k] = float(v)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w") as fh:
            yaml_rt.dump(data, fh)
        tmp.replace(path)
        log.info("Persisted head_tracking params to %s (ruamel)", path)
        return True
    except ImportError:
        # Fallback: PyYAML (loses comments)
        import yaml
        try:
            with open(path, "r") as fh:
                data = yaml.safe_load(fh) or {}
            if not isinstance(data, dict):
                return False
            ht = data.get("head_tracking")
            if not isinstance(ht, dict):
                ht = {}
                data["head_tracking"] = ht
            for k, v in params.items():
                ht[k] = float(v)
            tmp = path.with_suffix(path.suffix + ".tmp")
            with open(tmp, "w") as fh:
                yaml.safe_dump(data, fh, sort_keys=False)
            tmp.replace(path)
            log.warning("Persisted head_tracking params to %s (PyYAML — comments lost)", path)
            return True
        except Exception:
            log.exception("Failed to persist head_tracking params")
            return False
    except Exception:
        log.exception("Failed to persist head_tracking params")
        return False
