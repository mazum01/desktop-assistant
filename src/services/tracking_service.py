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
tracking.save_params         ``{}``  — persist current config to assistant.yaml
tracking.apply_preset        ``{"preset": str}``  — apply a named HeadTracker preset
tracking.reset_params        ``{}``  — reset to "default" preset
av.speaking_started ``{"text": str, "ts": float}``
av.spoke            ``{"text": str, "ts": float}``

Topics published
----------------
motion.pan_to       ``{angle: float, move_time_ms: float}``
motion.tilt_to      Same payload, only when tilt hardware is configured/ready
tracking.face_tracking_changed   ``{"enabled": bool}``
tracking.random_motion_changed   ``{"enabled": bool}``
tracking.person_seek_changed     ``{"enabled": bool}``
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import replace
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.motion.head_tracker import PRESETS, TUNABLE_FIELDS, HeadTracker, HeadTrackerConfig

log = logging.getLogger(__name__)

_UPDATE_HZ = 20
_UPDATE_INTERVAL = 1.0 / _UPDATE_HZ

# Maximum age (seconds) of a person detection hint before it is discarded.
# At 2 fps object detection this equals ~4 missed frames.
_PERSON_SEEK_STALE_S = 2.0
_FACE_STALE_S = 2.0
_TILT_MAX_OFFSET_DEG = 20.0


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
        self._current_face_cy: Optional[float] = None
        self._face_ts: float = 0.0
        self._current_servo_angle: Optional[float] = None
        self._current_tilt_angle: Optional[float] = None
        self._tilt_tracker: Optional[HeadTracker] = None
        # Person-seek state: pixel x-centre from latest "person" detection
        self._person_cx: Optional[float] = None
        self._person_cx_ts: float = 0.0
        self._lock = threading.Lock()
        self._unsubs: list = []

    @property
    def face_tracking_enabled(self) -> bool:
        return self._face_tracking_enabled

    @property
    def random_motion_enabled(self) -> bool:
        return self._random_motion_enabled

    @property
    def person_seek_enabled(self) -> bool:
        return self._person_seek_enabled

    def update_frame_width(self, frame_width: int, frame_height: Optional[int] = None) -> None:
        """Update the horizontal pixel width used for tracking math.

        Called when camera rotation changes so the tracker re-centres
        correctly without requiring a daemon restart.
        """
        with self._lock:
            self._tracker_cfg.frame_width = frame_width
            if frame_height is not None:
                self._tracker_cfg.frame_height = frame_height
            if self._tracker is not None:
                self._tracker._cfg.frame_width = frame_width
            self._update_tilt_geometry()
        log.info("TrackingService: frame_width updated to %d", frame_width)

    def _update_tilt_geometry(self) -> None:
        if self._tilt_tracker is not None:
            cfg = self._tracker_cfg
            vertical_fov = math.degrees(2 * math.atan(
                math.tan(math.radians(cfg.fov_degrees) / 2)
                * cfg.frame_height / cfg.frame_width
            ))
            self._tilt_tracker._cfg.frame_width = cfg.frame_height
            self._tilt_tracker._cfg.fov_degrees = vertical_fov

    # ── Tunable params API (called by web_service.py) ────────────────────

    def get_tunable_params(self) -> dict:
        """Return current tracker config, valid ranges, and preset names."""
        with self._lock:
            params = self._tracker.get_config() if self._tracker else {
                k: float(getattr(self._tracker_cfg, k, 0.0))
                for k in TUNABLE_FIELDS
            }
        return {
            "params": params,
            "ranges": TUNABLE_FIELDS,
            "presets": list(PRESETS.keys()),
        }

    def set_tunable_param(self, name: str, value: float) -> bool:
        """Update a single tracker parameter at runtime. Returns True on success."""
        with self._lock:
            if self._tracker is not None:
                applied = self._tracker.update_config(name, value)
                if applied and self._tilt_tracker is not None:
                    self._tilt_tracker.update_config(name, value)
                return applied
            # Tracker not yet started — mutate the config directly.
            if name not in TUNABLE_FIELDS:
                return False
            lo, hi = TUNABLE_FIELDS[name]
            v = float(value)
            if not (lo <= v <= hi):
                return False
            setattr(self._tracker_cfg, name, v)
            return True

    def on_start(self) -> None:
        if not self._enabled:
            log.info("TrackingService disabled via config")
            return

        self._tracker = HeadTracker(
            initial_servo_angle=self._tracker_cfg.servo_center,
            config=self._tracker_cfg,
        )

        self._unsubs.append(self.bus.subscribe("perception.faces", self._on_faces))
        self._unsubs.append(self.bus.subscribe("perception.objects", self._on_objects))
        self._unsubs.append(self.bus.subscribe("motion.position", self._on_position))
        self._unsubs.append(self.bus.subscribe("motion.limits_changed", self._on_motion_limits))
        self._unsubs.append(self.bus.subscribe("motion.settings", self._on_motion_settings))
        self._unsubs.append(self.bus.subscribe("tracking.set_face_tracking", self._on_set_face_tracking))
        self._unsubs.append(self.bus.subscribe("tracking.set_random_motion", self._on_set_random_motion))
        self._unsubs.append(self.bus.subscribe("tracking.set_person_seek", self._on_set_person_seek))
        self._unsubs.append(self.bus.subscribe("av.speaking_started", self._on_speaking_started))
        self._unsubs.append(self.bus.subscribe("av.spoke", self._on_spoke))
        self._unsubs.append(self.bus.subscribe("tracking.save_params", self._on_save_params))
        self._unsubs.append(self.bus.subscribe("tracking.apply_preset", self._on_apply_preset))
        self._unsubs.append(self.bus.subscribe("tracking.reset_params", self._on_reset_params))

        settings = self.bus.last("motion.settings")
        if isinstance(settings, dict):
            self._on_motion_settings("motion.settings", settings)
        log.info(
            "TrackingService started (%d Hz, enabled=%s, face_tracking=%s, random_motion=%s, person_seek=%s)",
            _UPDATE_HZ, self._enabled, self._face_tracking_enabled,
            self._random_motion_enabled, self._person_seek_enabled,
        )

    def on_stop(self) -> None:
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
                self._current_face_cy = None
        self.bus.publish("tracking.face_tracking_changed", {"enabled": new_state})

    def _on_motion_limits(self, _topic, payload) -> None:
        with self._lock:
            self._tracker_cfg.servo_min = payload["min_deg"]
            self._tracker_cfg.servo_max = payload["max_deg"]
            self._tracker_cfg.servo_center = (payload["min_deg"] + payload["max_deg"]) / 2

    def _on_motion_settings(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._lock:
            if not payload.get("tilt_available", False):
                self._tilt_tracker = None
                return
            try:
                mn, mx = float(payload["tilt_min_deg"]), float(payload["tilt_max_deg"])
                if not (math.isfinite(mn) and math.isfinite(mx) and 0 <= mn < mx <= 180):
                    raise ValueError("Tilt limits must fit 0-180 degrees")
            except (KeyError, TypeError, ValueError):
                log.error("Invalid tilt capability settings: %r", payload)
                self._tilt_tracker = None
                return
            center = (mn + mx) / 2
            cfg = replace(
                self._tracker_cfg,
                servo_min=max(mn, center - _TILT_MAX_OFFSET_DEG),
                servo_max=min(mx, center + _TILT_MAX_OFFSET_DEG),
                servo_center=center,
                invert_pan=self._tracker_cfg.invert_tilt,
                saccade_amplitude_deg=0,
            )
            initial = self._current_tilt_angle
            self._tilt_tracker = HeadTracker(
                initial_servo_angle=center if initial is None else initial,
                config=cfg,
            )
            self._update_tilt_geometry()

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
                self._current_face_cy = None
            return
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        width, height = payload.get("frame_w"), payload.get("frame_h")
        if (isinstance(width, int) and isinstance(height, int)
                and width > 0 and height > 0
                and (width, height) != (
                    self._tracker_cfg.frame_width, self._tracker_cfg.frame_height,
                )):
            self.update_frame_width(width, height)
        if faces:
            primary = max(faces, key=lambda f: f.get("confidence", 0.0))
            cx, cy = primary.get("centroid", [None, None])
            with self._lock:
                self._current_face_cx = float(cx) if cx is not None else None
                self._current_face_cy = float(cy) if cy is not None else None
                self._face_ts = time.monotonic()
        else:
            with self._lock:
                self._current_face_cx = None
                self._current_face_cy = None

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
            self._person_cx = cx * self._tracker_cfg.frame_width / frame_w
            self._person_cx_ts = time.monotonic()

    def _on_position(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._lock:
                self._current_servo_angle = float(payload["angle"])
                tilt = payload.get("tilt_angle")
                self._current_tilt_angle = float(tilt) if tilt is not None else None

    def _on_speaking_started(self, _topic, _payload) -> None:
        """Mark the head as 'speaking' until av.spoke arrives (max 60 s safety cap)."""
        self._speaking_until = time.monotonic() + 60.0

    def _on_spoke(self, _topic, _payload) -> None:
        """Clear the speaking window as soon as TTS finishes."""
        self._speaking_until = 0.0

    def _on_save_params(self, _topic, _payload) -> None:
        """Persist current tracker config to config/assistant.yaml."""
        import os
        import yaml
        cfg_path = os.path.join(os.path.dirname(__file__), "..", "..", "config", "assistant.yaml")
        cfg_path = os.path.normpath(cfg_path)
        try:
            with open(cfg_path) as f:
                full_cfg = yaml.safe_load(f) or {}
            with self._lock:
                params = self._tracker.get_config() if self._tracker else {}
            ht = full_cfg.setdefault("head_tracking", {})
            ht.update(params)
            with open(cfg_path, "w") as f:
                yaml.dump(full_cfg, f, default_flow_style=False, sort_keys=False)
            log.info("TrackingService: params saved to %s", cfg_path)
        except Exception as exc:
            log.error("TrackingService: failed to save params: %s", exc)

    def _on_apply_preset(self, _topic, payload) -> None:
        """Apply a named preset from PRESETS."""
        if not isinstance(payload, dict):
            return
        name = payload.get("preset")
        if name not in PRESETS:
            log.warning("TrackingService: unknown preset %r", name)
            return
        with self._lock:
            for k, v in PRESETS[name].items():
                if self._tracker is not None:
                    self._tracker.update_config(k, v)
                    if self._tilt_tracker is not None:
                        self._tilt_tracker.update_config(k, v)
                else:
                    setattr(self._tracker_cfg, k, v)
        log.info("TrackingService: applied preset %r", name)

    def _on_reset_params(self, _topic, _payload) -> None:
        """Reset all tunable params to the 'default' preset."""
        self._on_apply_preset(None, {"preset": "default"})
        log.info("TrackingService: params reset to default")

    # ── Tracking loop ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        if not self._enabled:
            self._stop_event.wait()
            return
        last_t = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            dt = now - last_t
            last_t = now

            with self._lock:
                # When face tracking is disabled, pass None so the tracker
                # goes to idle mode. When random motion is also disabled,
                # skip publishing pan_to entirely so the head stays put.
                fresh_face = self._face_tracking_enabled and now - self._face_ts < _FACE_STALE_S
                face_cx = self._current_face_cx if fresh_face else None
                face_cy = self._current_face_cy if fresh_face else None
                servo_angle = self._current_servo_angle
                tilt_angle = self._current_tilt_angle
                # Person-seek: use person centroid as a soft target only
                # when no face is currently locked and the hint is fresh.
                person_cx = None
                if (
                    face_cx is None
                    and self._face_tracking_enabled
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
            pan_payload: dict = {
                "angle": round(target, 2),
                "move_time_ms": round(move_ms, 1),
            }
            # Face tracking and person-seek respond to human presence — they
            # should work even during quiet hours.  Random idle motion should
            # stay suppressed (it's the noise-making autonomous behaviour the
            # user is silencing).
            if effective_cx is not None:
                pan_payload["override_quiet"] = True
            self.bus.publish("motion.pan_to", pan_payload)
            with self._lock:
                if (self._tilt_tracker is not None and face_cy is not None
                        and tilt_angle is not None):
                    tilt_target = self._tilt_tracker.update(
                        face_cx=face_cy, dt=dt, current_servo_angle=tilt_angle,
                    )
                else:
                    tilt_target = None
            if tilt_target is not None:
                self.bus.publish("motion.tilt_to", {
                    "angle": round(tilt_target, 2),
                    "move_time_ms": round(move_ms, 1),
                    "override_quiet": True,
                })

            elapsed = time.monotonic() - now
            sleep_t = max(0.001, _UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_t)


# ── Auto-tune helpers (module-level, testable) ────────────────────────────────

def _analyse_response(
    samples: list[tuple[float, float, float]],
) -> tuple[float, float]:
    """Estimate servo lag and overshoot from a list of (face_cx, servo_angle, t) samples.

    Uses cross-correlation on normalised signals to find the time offset where
    the servo best matches the face trajectory.

    Returns
    -------
    lag_s : float
        Estimated lag in seconds (positive = servo lags face).
    overshoot : float
        Ratio of servo peak excursion to face peak excursion (>1 = overshoot).
    """
    if not samples:
        return 0.0, 0.0

    import numpy as np

    face_vals = np.array([s[0] for s in samples], dtype=np.float64)
    servo_vals = np.array([s[1] for s in samples], dtype=np.float64)
    times = np.array([s[2] for s in samples], dtype=np.float64)

    dt = float(np.mean(np.diff(times))) if len(times) > 1 else 0.05

    # Normalise to zero-mean unit variance
    def _norm(x: "np.ndarray") -> "np.ndarray":
        std = x.std()
        return (x - x.mean()) / std if std > 1e-9 else np.zeros_like(x)

    face_n = _norm(face_vals)
    servo_n = _norm(servo_vals)

    # Full cross-correlation; centre index = zero lag
    corr = np.correlate(servo_n, face_n, mode="full")
    centre = len(face_n) - 1
    lag_samples = int(np.argmax(corr)) - centre
    lag_s = float(lag_samples * dt)

    # Overshoot: peak servo excursion vs peak face excursion
    face_range = float(np.ptp(face_vals))
    servo_range = float(np.ptp(servo_vals))
    overshoot = servo_range / face_range if face_range > 1e-6 else 1.0

    return lag_s, overshoot


def _persist_head_tracking_params(path: "Path | str", params: dict) -> bool:
    """Update *only* the ``head_tracking`` keys listed in *params* in a YAML config file.

    Preserves all other sections and comments (uses ruamel.yaml round-trip loader).

    Returns True on success, False on any error.
    """
    from pathlib import Path as _Path
    try:
        from ruamel.yaml import YAML
        yaml = YAML()
        yaml.preserve_quotes = True
        p = _Path(path)
        data = yaml.load(p)
        if data is None:
            data = {}
        if "head_tracking" not in data:
            data["head_tracking"] = {}
        for k, v in params.items():
            data["head_tracking"][k] = v
        yaml.dump(data, p)
        return True
    except Exception:
        return False
