"""
Head tracking service.

Associates a stable face across timestamped observations and uses bounded
visual feedback after each completed move. Never feeds a cached detection
back into the controller as a new measurement. Optional plant calibration
fits repeated small probes and must pass live centering before persistence.

**Person seek** — when object detection is enabled and a ``person`` bounding
box appears on ``perception.objects`` but no face has been acquired yet, the
tracker uses the largest person's centroid as a stable seek target. This lets
the head pan (and tilt, where available) toward the person so SCRFD can pick up
their face.  Once a face locks on, face tracking takes over immediately; person
seek is inactive while a face is tracked.  Person seek is independent of the
face-tracking switch: with face tracking off the head still follows the person.

Controllers (selected per body in ``config/body_profiles.yaml``):

* ``legacy`` — Kalman/minimum-jerk ``HeadTracker`` for pan-only face tracking
  (original body).
* ``gaze`` — person-like pan/tilt following (new body): Kalman-filtered
  absolute target bearings drive continuous minimum-jerk motion with smooth
  pursuit and a fixation dead-band. Faces take priority over person seek.
* ``visual_servo`` — one bounded correction per fresh, post-settling frame,
  with optional plant calibration.

Topics subscribed
-----------------
perception.faces    ``{faces: [{centroid, confidence, …}], …}``
perception.objects  ``{objects: [...], persons: [{label, bbox:[x1,y1,x2,y2], …}], frame_w, frame_h, …}``
object.enabled_changed  Person seek requires object detection to remain enabled
motion.position     Last commanded angles, not measured shaft feedback
tracking.start_autotune / cancel_autotune   Explicit calibration controls
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
motion.calibration_lock     Expiring ownership lease during announced probes
tracking.debug / autotune_progress / autotune_done
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
from pathlib import Path
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.motion.gaze_tracker import AngleHistory, GazeConfig, GazeTracker, image_offsets
from src.motion.head_tracker import PRESETS, TUNABLE_FIELDS, HeadTracker, HeadTrackerConfig
from src.motion.visual_servo import (
    FaceLock, PlantModel, VisualController, VisualServoConfig, face_priority, identify_plant,
)
import numpy as np

log = logging.getLogger(__name__)

_UPDATE_HZ = 20
_UPDATE_INTERVAL = 1.0 / _UPDATE_HZ

# Maximum age (seconds) of a person detection hint before it is discarded.
# At 2 fps object detection this equals ~4 missed frames.
_PERSON_SEEK_STALE_S = 2.0
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
        visual_config: Optional[dict] = None,
        controller: str = "visual_servo",
        body: str = "new",
        params_path: Optional[Path] = None,
        gaze_config: Optional[dict] = None,
    ) -> None:
        super().__init__(bus=bus)
        if controller not in ("legacy", "visual_servo", "gaze"):
            raise ValueError(f"Unknown tracking controller: {controller!r}")
        self._controller = controller
        self._body = body
        self._params_path = params_path or Path(__file__).parents[2] / "config" / "assistant.yaml"
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
        self._face_ts: float = -math.inf
        self._current_servo_angle: Optional[float] = None
        self._current_tilt_angle: Optional[float] = None
        self._tilt_tracker: Optional[HeadTracker] = None
        # Person-seek state: pixel x-centre from latest "person" detection
        self._person_cx: Optional[float] = None
        self._person_cx_ts: float = 0.0
        self._person_cy: Optional[float] = None
        self._tilt_person_tracking = False
        self._person_center: Optional[np.ndarray] = None
        self._person_last_command = -math.inf
        self._person_ready_at = -math.inf
        self._object_detection_enabled = False
        self._lock = threading.Lock()
        self._unsubs: list = []
        self._visual_cfg = VisualServoConfig(**(visual_config or {}))
        self._face_lock = FaceLock(self._visual_cfg)
        self._visual = VisualController(self._visual_cfg)
        self._observation = None
        self._motion_settings: dict = {}
        self._rotation = 0
        self._plant: Optional[PlantModel] = None
        self._calibration_signature = None
        self._calibration = None
        self._calibration_requested = False
        self._cancel_calibration = False
        self._faces_present = False
        self._gaze = GazeTracker(self._tracker_cfg, GazeConfig(**(gaze_config or {})))
        self._angle_history = AngleHistory()
        self._gaze_sent: dict[str, float] = {}

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
            self._face_lock.reset()
            self._observation = None
            if self._tracker is not None:
                self._tracker._cfg.frame_width = frame_width
            self._update_tilt_geometry()
        log.info("TrackingService: frame_width updated to %d", frame_width)

    def _update_tilt_geometry(self) -> None:
        if self._tilt_tracker is not None:
            cfg = self._tracker_cfg
            self._tilt_tracker._cfg.frame_width = cfg.frame_height
            self._tilt_tracker._cfg.fov_degrees = cfg.vertical_fov()

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
            "body": self._body,
            "controller": self._controller,
            "controller_description": {
                "legacy": "Legacy Kalman/minimum-jerk pan tracking",
                "gaze": ("Person-like gaze: Kalman-filtered target bearing with "
                         "minimum-jerk pan/tilt motion and smooth pursuit; "
                         "tracking_gain applies to idle motion only"),
            }.get(self._controller,
                  "Fresh-frame visual servo; legacy parameters apply to idle motion"),
            "calibrated": self._plant is not None,
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
        self._unsubs.append(self.bus.subscribe("object.enabled_changed", self._on_object_enabled))
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
        self._unsubs.append(self.bus.subscribe("tracking.start_autotune", self._on_start_calibration))
        self._unsubs.append(self.bus.subscribe("tracking.cancel_autotune", self._on_cancel_calibration))

        settings = self.bus.last("motion.settings")
        if isinstance(settings, dict):
            self._on_motion_settings("motion.settings", settings)
        objects = self.bus.last("object.enabled_changed")
        if isinstance(objects, dict):
            self._on_object_enabled("object.enabled_changed", objects)
        log.info(
            "TrackingService started (%d Hz, enabled=%s, face_tracking=%s, random_motion=%s, person_seek=%s, body=%s, controller=%s)",
            _UPDATE_HZ, self._enabled, self._face_tracking_enabled,
            self._random_motion_enabled, self._person_seek_enabled,
            self._body, self._controller,
        )

    def on_stop(self) -> None:
        if self._calibration is not None:
            self._finish_calibration("Service stopping; calibration discarded")
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
                self._face_lock.reset()
                self._observation = None
                self._face_ts = -math.inf
            self.bus.publish("motion.stop", {})
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
            self._motion_settings = dict(payload)
            if self._controller == "legacy" or not payload.get("tilt_available", False):
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
                invert_pan=True,
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
                self._person_cy = None
                self._person_cx_ts = 0.0
                self._person_center = None
        self.bus.publish("tracking.person_seek_changed", {"enabled": new_state})

    def _on_object_enabled(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or type(payload.get("enabled")) is not bool:
            log.error("Invalid object detection state: %r", payload)
            return
        with self._lock:
            had_person = self._person_cx is not None
            self._object_detection_enabled = payload["enabled"]
            if not self._object_detection_enabled:
                self._person_cx = self._person_cy = None
                self._person_center = None
                self._person_cx_ts = 0.0
        if (not payload["enabled"] and had_person and self._calibration is None
                and not self._face_in_priority(time.monotonic())):
            self.bus.publish("motion.stop", {})

    def _motion_ready(self) -> bool:
        return bool(self._motion_settings.get("servo_enabled", False)
                    and self._motion_settings.get("hardware_ready", False))

    def _face_in_priority(self, now: float) -> bool:
        return (self._face_tracking_enabled
                and now - self._face_ts < self._visual_cfg.face_handoff_s)

    def _on_faces(self, _topic, payload) -> None:
        if not self._face_tracking_enabled:
            with self._lock:
                self._current_face_cx = None
                self._current_face_cy = None
            return
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        self._faces_present = bool(faces)
        width, height = payload.get("frame_w"), payload.get("frame_h")
        if (isinstance(width, int) and isinstance(height, int)
                and width > 0 and height > 0
                and (width, height) != (
                    self._tracker_cfg.frame_width, self._tracker_cfg.frame_height,
                )):
            self.update_frame_width(width, height)
        now = time.monotonic()
        if self._controller == "legacy":
            primary = max(faces, key=face_priority) if faces else None
            centroid = primary.get("centroid") if primary else None
            if primary is not None and (
                not isinstance(centroid, (list, tuple)) or len(centroid) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) for v in centroid)
            ):
                log.warning("Legacy tracking holds: invalid face centroid %r", centroid)
                centroid = None
            stamp = payload.get("captured_mono", now)
            if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
                    or not 0 <= now - stamp < self._visual_cfg.max_age_s):
                log.warning("Legacy tracking holds: invalid or stale acquisition time %r", stamp)
                centroid = None
            with self._lock:
                self._current_face_cx = float(centroid[0]) if centroid else None
                self._current_face_cy = float(centroid[1]) if centroid else None
                if centroid:
                    self._face_ts = float(stamp)
            return
        stamp = payload.get("captured_mono")
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp):
            log.warning("Tracking holds: face result has no valid acquisition timestamp")
            return
        with self._lock:
            selected = self._face_lock.select(
                faces, self._tracker_cfg.frame_width, self._tracker_cfg.frame_height,
                stamp, now,
            )
            self._rotation = payload.get("rotation_deg", 0)
            if selected is not None:
                face, point = selected
                cx, cy = face["centroid"]
                self._current_face_cx = float(cx)
                self._current_face_cy = float(cy)
                self._face_ts = float(stamp)
                self._observation = (point.copy(), float(stamp), now, len(faces))
            elif stamp > (self._observation[1] if self._observation else -math.inf):
                self._current_face_cx = self._current_face_cy = None
                self._observation = None

    def _on_objects(self, _topic, payload) -> None:
        """Update person-seek hint from object detection."""
        if not self._person_seek_enabled or not self._object_detection_enabled:
            return
        if not isinstance(payload, dict):
            return
        # "persons" keeps person boxes the overlay hides while a face is visible.
        candidates = payload["persons"] if isinstance(payload.get("persons"), list) \
            else payload.get("objects") or []
        frame_w = payload.get("frame_w", 0)
        if not frame_w:
            return
        # Use normalized centers so association is independent of detection size.
        persons = [o for o in candidates if o.get("label") == "person"]
        if not persons:
            with self._lock:
                self._person_cx = None
                self._person_cy = None
                self._person_cx_ts = 0.0
                self._person_center = None
            return
        def area(person):
            box = person["bbox"]
            return (box[2] - box[0]) * (box[3] - box[1])

        frame_h = payload.get("frame_h", 0)
        for person in persons:
            box = person.get("bbox")
            if (not isinstance(box, (list, tuple)) or len(box) != 4
                    or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                    or not 0 <= box[0] < box[2] <= frame_w
                    or not 0 <= box[1] < box[3]
                    or (frame_h and box[3] > frame_h)):
                log.warning("Person tracking holds: invalid bounding box %r", box)
                return
        now = time.monotonic()
        stamp = payload.get("captured_mono", now)
        if (not isinstance(stamp, (int, float)) or not math.isfinite(stamp)
                or not 0 <= now - stamp < _PERSON_SEEK_STALE_S):
            log.warning("Person tracking holds: invalid or stale acquisition time %r", stamp)
            return
        best = max(persons, key=area)
        if self._person_center is not None and now - self._person_cx_ts < _PERSON_SEEK_STALE_S:
            def distance(person):
                box = person["bbox"]
                center = np.array([(box[0] + box[2]) / (2 * frame_w),
                                   (box[1] + box[3]) / (2 * frame_h) if frame_h else 0])
                return float(np.linalg.norm(center - self._person_center))
            incumbent = min(persons, key=distance)
            if (distance(incumbent) <= self._visual_cfg.association_radius
                    and area(best) <= area(incumbent) * (1 + self._visual_cfg.person_switch_margin)):
                best = incumbent
        bbox = best.get("bbox")
        if not bbox or len(bbox) < 4:
            return
        cx = (bbox[0] + bbox[2]) / 2.0
        cy = (
            (bbox[1] + bbox[3]) / 2.0 * self._tracker_cfg.frame_height / frame_h
            if frame_h else None
        )
        with self._lock:
            self._person_cx = cx * self._tracker_cfg.frame_width / frame_w
            self._person_cy = cy
            self._person_cx_ts = float(stamp)
            self._person_center = np.array([cx / frame_w,
                                           (bbox[1] + bbox[3]) / (2 * frame_h) if frame_h else 0])

    def _on_position(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._lock:
                self._current_servo_angle = float(payload["angle"])
                tilt = payload.get("tilt_angle")
                self._current_tilt_angle = float(tilt) if tilt is not None else None
                self._angle_history.add(
                    time.monotonic(), self._current_servo_angle, self._current_tilt_angle,
                )

    def _on_speaking_started(self, _topic, _payload) -> None:
        """Mark the head as 'speaking' until av.spoke arrives (max 60 s safety cap)."""
        self._speaking_until = time.monotonic() + 60.0

    def _on_spoke(self, _topic, _payload) -> None:
        """Clear the speaking window as soon as TTS finishes."""
        self._speaking_until = 0.0

    def _on_save_params(self, _topic, _payload) -> None:
        """Persist tuning to the selected body's local identity file."""
        import yaml
        cfg_path = self._params_path
        try:
            if cfg_path.name == "body.yaml" and not cfg_path.exists():
                full_cfg = {"body": self._body}
            else:
                full_cfg = yaml.safe_load(cfg_path.read_text()) or {}
            if cfg_path.name == "body.yaml" and full_cfg.get("body") != self._body:
                raise ValueError("Body identity changed; restart before saving tracking parameters")
            with self._lock:
                params = self._tracker.get_config() if self._tracker else {}
            ht = full_cfg.setdefault("head_tracking", {})
            ht.update(params)
            temporary = cfg_path.with_suffix(".yaml.tmp")
            temporary.write_text(yaml.safe_dump(full_cfg, sort_keys=False))
            temporary.replace(cfg_path)
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
            try:
                if self._controller == "legacy":
                    self._legacy_tick(now, max(0.001, now - last_t))
                elif self._controller == "gaze":
                    self._gaze_tick(now, min(0.2, max(0.001, now - last_t)))
                else:
                    self._visual_tick(now)
            except (ValueError, OSError, KeyError, TypeError):
                log.exception("%s tracking failed; holding position", self._controller)
                self.bus.publish("motion.stop", {})
                if self._calibration is not None:
                    self._finish_calibration("Calibration failed; previous model retained")
            last_t = now
            self._stop_event.wait(_UPDATE_INTERVAL)

    def _legacy_tick(self, now: float, dt: float) -> None:
        if not self._motion_ready():
            return
        with self._lock:
            tracker = self._tracker
            if tracker is None:
                return
            face = (
                self._current_face_cx
                if self._face_tracking_enabled and now - self._face_ts < self._visual_cfg.face_handoff_s
                else None
            )
            person = (
                self._person_cx
                if self._person_seek_enabled and self._object_detection_enabled
                and now - self._person_cx_ts < _PERSON_SEEK_STALE_S else None
            )
            if face is None and self._face_in_priority(now):
                return
            target_cx = face if face is not None else person
            if target_cx is None and not self._random_motion_enabled:
                return
            target = tracker.update(target_cx, dt, self._current_servo_angle)
            if self._speaking_motion_enabled and now < self._speaking_until:
                target += self._speaking_motion_amplitude * math.sin(
                    2 * math.pi * self._speaking_motion_freq * now
                )
            target = max(self._tracker_cfg.servo_min, min(self._tracker_cfg.servo_max, target))
            debug = tracker.get_debug_state()
        self.bus.publish("motion.pan_to", {
            "angle": round(target, 2), "move_time_ms": _UPDATE_INTERVAL * 2000,
            "override_quiet": target_cx is not None,
        })
        self.bus.publish("tracking.debug", {
            **debug, "body": self._body, "controller": self._controller,
        })

    def _gaze_tick(self, now: float, dt: float) -> None:
        if not self._motion_ready():
            self._gaze.halt()
            self._gaze_sent.clear()
            return
        cfg = self._tracker_cfg
        with self._lock:
            positions, limits = self._positions_and_limits()
            observation = self._observation if self._face_tracking_enabled else None
            if observation is not None and now - observation[1] > self._visual_cfg.max_age_s:
                observation = None
            face_active = self._face_in_priority(now)
            person = None
            if (not face_active and self._person_seek_enabled
                    and self._object_detection_enabled and self._person_cx is not None
                    and now - self._person_cx_ts < _PERSON_SEEK_STALE_S):
                py = (self._person_cy / cfg.frame_height - 0.5
                      if self._person_cy is not None else math.nan)
                person = ((self._person_cx / cfg.frame_width - 0.5, py), self._person_cx_ts)
        if positions is None:
            return
        names = ("pan", "tilt")[:len(positions)]
        bounds = {n: (float(lo), float(hi)) for n, (lo, hi) in zip(names, limits)}
        source = "face" if face_active else "person" if person is not None else None
        if source is None:
            self._gaze.release()
            if self._gaze.moving:
                self._publish_gaze(self._gaze.step(now, dt, bounds), now, tracking=False)
                return
            self._gaze_sent.clear()
            self._idle_tick(now, observation)
            return
        self._gaze.engage(source, {n: float(p) for n, p in zip(names, positions)})
        sample = (observation[0], observation[1]) if source == "face" and observation else person
        if sample is not None and sample[1] > self._gaze.last_stamp():
            point, stamp = sample
            with self._lock:
                head = self._angle_history.at(stamp - self._gaze.gaze.servo_lag_s)
            pan_at, tilt_at = head if head is not None else (positions[0], None)
            if tilt_at is None and len(positions) > 1:
                tilt_at = positions[1]
            pan_off, tilt_off = image_offsets(point, cfg)
            bearings = {"pan": pan_at + pan_off}
            if tilt_at is not None and math.isfinite(point[1]):
                bearings["tilt"] = tilt_at + tilt_off
            self._gaze.observe(bearings, stamp)
        self._publish_gaze(self._gaze.step(now, dt, bounds), now, tracking=True)

    def _publish_gaze(self, targets: dict, now: float, tracking: bool) -> None:
        targets = dict(targets)
        if (tracking and "pan" in targets and self._speaking_motion_enabled
                and now < self._speaking_until):
            lo, hi = self._tracker_cfg.servo_min, self._tracker_cfg.servo_max
            targets["pan"] = max(lo, min(hi, targets["pan"] + self._speaking_motion_amplitude
                                         * math.sin(2 * math.pi * self._speaking_motion_freq * now)))
        for axis, angle in targets.items():
            if abs(angle - self._gaze_sent.get(axis, math.inf)) < 0.01:
                continue
            self._gaze_sent[axis] = angle
            self.bus.publish(f"motion.{axis}_to", {
                "angle": round(float(angle), 2), "move_time_ms": _UPDATE_INTERVAL * 2000,
                "override_quiet": True, "source": "gaze_tracking",
            })
        self.bus.publish("tracking.debug", {
            **self._gaze.debug_state(), "body": self._body, "controller": self._controller,
            "face_raw": self._current_face_cx, "face_smoothed": self._current_face_cx,
        })

    def _signature(self):
        keys = ("pan_channel", "tilt_channel", "range_deg", "soft_min_deg",
                "soft_max_deg", "tilt_min_deg", "tilt_max_deg", "tilt_available",
                "pulse_min_us", "pulse_max_us")
        return {
            "frame": [self._tracker_cfg.frame_width, self._tracker_cfg.frame_height],
            "rotation": self._rotation,
            "servo": {k: self._motion_settings.get(k) for k in keys},
            "tilt_convention": 2,
            "direction": {
                "invert_pan": self._tracker_cfg.invert_pan,
                "invert_tilt": self._tracker_cfg.invert_tilt,
            },
        }

    def _calibration_path(self):
        path = Path(self._visual_cfg.calibration_path)
        return path if path.is_absolute() else Path(__file__).parents[2] / path

    def _model(self):
        signature = self._signature()
        if self._calibration_signature != signature:
            self._plant = None
            self._calibration_signature = signature
            path = self._calibration_path()
            if path.exists():
                try:
                    self._plant = PlantModel.load(path, signature)
                except (ValueError, TypeError, OSError):
                    log.warning("Ignoring incompatible calibration; using conservative model", exc_info=True)
            self._visual = VisualController(self._visual_cfg)
        if self._plant is not None:
            return self._plant
        cfg = self._tracker_cfg
        # Normalized image displacement per commanded degree; empirical
        # calibration replaces this approximation. Tilt angles are logical (above
        # 90 looks up), so a higher angle moves a fixed target down in the image.
        pan = (1 if cfg.invert_pan else -1) / cfg.fov_degrees
        matrix = [[pan], [0.0]]
        if self._motion_settings.get("tilt_available"):
            matrix = [[pan, 0.0], [0.0, 1 / cfg.vertical_fov()]]
        return PlantModel(matrix, self._visual_cfg.settle_s, 0, signature)

    def _positions_and_limits(self):
        positions = [self._current_servo_angle]
        limits = [[self._tracker_cfg.servo_min, self._tracker_cfg.servo_max]]
        if self._motion_settings.get("tilt_available"):
            positions.append(self._current_tilt_angle)
            mn = self._motion_settings["tilt_min_deg"]
            mx = self._motion_settings["tilt_max_deg"]
            center = (mn + mx) / 2
            limits.append([max(mn, center - 20), min(mx, center + 20)])
        if any(p is None for p in positions):
            return None, None
        return np.asarray(positions, dtype=float), np.asarray(limits, dtype=float)

    def _send_targets(self, targets, duration):
        for axis, angle in zip(("pan", "tilt"), targets):
            self.bus.publish(f"motion.{axis}_to", {
                "angle": float(angle), "move_time_ms": duration * 1000,
                "profile": "smoothstep", "override_quiet": True,
                "source": "calibration" if self._calibration is not None else "visual_tracking",
            })

    def _visual_tick(self, now):
        with self._lock:
            observation = self._observation
            positions, limits = self._positions_and_limits()
            active = self._face_tracking_enabled and self._motion_ready()
            if observation is not None and now - observation[1] > self._visual_cfg.max_age_s:
                observation = None
        if self._calibration is not None:
            self._calibration_tick(now, observation, positions, limits)
            return
        if self._calibration_requested:
            self._calibration_requested = False
            self._begin_calibration(now, observation, positions, limits)
            return
        if not active or positions is None or observation is None:
            self._idle_tick(now, observation)
            return
        error, stamp, _, _ = observation
        model = self._model()
        result = self._visual.correction(error, positions, limits, stamp, now, model)
        if result is not None:
            self._send_targets(*result)
        self.bus.publish("tracking.debug", {
            "body": self._body, "controller": self._controller,
            "mode": self._visual.state, "face_raw": self._current_face_cx,
            "face_smoothed": self._current_face_cx, "face_vel": 0,
            "servo_angle": float(positions[0]), "target": float(positions[0]),
            "calibrated": self._plant is not None,
            "frame_age_s": now - stamp, "error": error.tolist(),
        })

    def _idle_tick(self, now, observation):
        if (not self._motion_ready()
                or (observation is not None and self._face_tracking_enabled)
                or self._tracker is None or self._calibration_requested
                or self._face_in_priority(now)):
            return
        person = person_cy = None
        with self._lock:
            tilt_tracker = self._tilt_tracker
            if (self._person_seek_enabled and self._object_detection_enabled
                    and now - self._person_cx_ts < _PERSON_SEEK_STALE_S):
                person, person_cy = self._person_cx, self._person_cy
        if person is None and self._tilt_person_tracking and tilt_tracker is not None:
            # Drop the tilt filter state so the next person starts from a clean estimate.
            tilt_tracker.update(None, _UPDATE_INTERVAL, self._current_tilt_angle)
            self._tilt_person_tracking = False
        if person is None and not self._random_motion_enabled:
            return
        # Legacy person seek is rate-limited to one correction per detection.
        if person is not None and (
            self._person_cx_ts <= self._person_last_command or now < self._person_ready_at
        ):
            return
        dt = _UPDATE_INTERVAL
        if person is not None:
            dt = min(self._tracker_cfg.move_max_s,
                     max(_UPDATE_INTERVAL, self._person_cx_ts - self._person_last_command))
            self._person_last_command = self._person_cx_ts
        target = self._tracker.update(
            person, dt, self._current_servo_angle,
        )
        if person is None and self._speaking_motion_enabled and now < self._speaking_until:
            target += self._speaking_motion_amplitude * math.sin(
                2 * math.pi * self._speaking_motion_freq * now
            )
        target = max(self._tracker_cfg.servo_min, min(self._tracker_cfg.servo_max, target))
        if person is None:
            self.bus.publish("motion.pan_to", {
                "angle": target, "move_time_ms": 500, "override_quiet": False,
            })
            return
        positions = [self._current_servo_angle if self._current_servo_angle is not None
                     else self._tracker_cfg.servo_center]
        targets = [target]
        if person is not None and person_cy is not None and tilt_tracker is not None:
            tilt_target = tilt_tracker.update(
                person_cy, dt, self._current_tilt_angle,
            )
            self._tilt_person_tracking = True
            positions.append(self._current_tilt_angle if self._current_tilt_angle is not None
                             else tilt_tracker._cfg.servo_center)
            targets.append(tilt_target)
        delta = np.clip(np.asarray(targets) - positions,
                        -self._visual_cfg.max_step_deg, self._visual_cfg.max_step_deg)
        duration = self._visual.move_duration(float(np.max(np.abs(delta))))
        self._send_targets(np.asarray(positions) + delta, duration)
        self._person_ready_at = now + duration + self._visual_cfg.settle_s

    def _on_start_calibration(self, _topic, _payload):
        if self._controller != "visual_servo":
            message = "Visual plant calibration is only used by the visual_servo controller"
            log.warning(message)
            self.bus.publish("tracking.autotune_done", {
                "ok": False, "error": message, "reason": message,
                "controller": self._controller,
            })
            return
        self._calibration_requested = True

    def _on_cancel_calibration(self, _topic, _payload):
        self._calibration_requested = False
        self._cancel_calibration = True

    def _begin_calibration(self, now, observation, positions, limits):
        if self._calibration is not None:
            return
        if (not self._face_tracking_enabled
                or not self._motion_settings.get("servo_enabled")
                or not self._motion_settings.get("hardware_ready")
                or positions is None or observation is None
                or observation[3] != 1
                or now - observation[1] > self._visual_cfg.max_age_s):
            self._finish_calibration("Calibration requires one locked face and enabled, ready servos")
            return
        step = self._visual_cfg.calibration_step_deg
        if np.any(positions - step < limits[:, 0]) or np.any(positions + step > limits[:, 1]):
            self._finish_calibration("Insufficient travel margin; manually center the head and retry")
            return
        plan = [positions.copy()]
        for axis in range(len(positions)):
            for direction in (1, -1, 1, -1):
                target = positions.copy()
                target[axis] += direction * step
                plan.extend([target, positions.copy()])
        self.bus.publish("motion.stop", {})
        self._cancel_calibration = False
        self._calibration = {
            "plan": plan, "index": 0, "phase": "announce", "deadline": now + 45,
            "commands": [], "points": [], "durations": [], "noise": [],
            "samples": [], "origin": positions.copy(), "signature": self._signature(),
            "last_stamp": observation[1], "last_position": positions.copy(),
            "lease_renewed": now,
        }
        self.bus.publish("motion.calibration_lock", {"enabled": True})
        self.bus.publish("tracking.autotune_progress", {
            "phase": "calibration", "message": "Keep one face still; small announced servo probes",
        })

    def _finish_calibration(self, error=None):
        self._calibration = None
        self._calibration_requested = False
        self._cancel_calibration = False
        self._visual = VisualController(self._visual_cfg)
        self._visual.ready_at = time.monotonic() + 2 * self._visual_cfg.settle_s
        self.bus.publish("motion.calibration_lock", {"enabled": False})
        self.bus.publish("motion.stop", {})
        if error:
            log.warning("Visual calibration: %s", error)
        self.bus.publish("tracking.autotune_done", {
            "ok": error is None, "error": error, "reason": error,
            "message": error or "Plant calibration saved and live centering validated",
            "controller": "visual_servo",
            "completed_mono": time.monotonic(),
        })

    def _calibration_tick(self, now, observation, positions, limits):
        state = self._calibration
        if state is None:
            return
        if now - state["lease_renewed"] > 5:
            self.bus.publish("motion.calibration_lock", {"enabled": True})
            state["lease_renewed"] = now
        if (self._cancel_calibration or not self._face_tracking_enabled
                or not self._motion_settings.get("servo_enabled")
                or not self._motion_settings.get("hardware_ready")
                or positions is None or state["signature"] != self._signature()):
            self._finish_calibration("Calibration cancelled or hardware configuration changed")
            return
        if now > state["deadline"]:
            self._finish_calibration("Calibration timed out; model not saved")
            return
        if (observation is None or observation[3] != 1
                or not 0 <= now - observation[1] <= self._visual_cfg.max_age_s):
            self._finish_calibration("Face lost, ambiguous, or stale; model not saved")
            return
        if state["phase"] == "validate":
            self._validate_calibration(now, observation, positions, limits)
            return
        if state["phase"] == "validation_speech":
            spoke = self.bus.last("av.spoke")
            if isinstance(spoke, dict) and spoke.get("request_id") == state["token"]:
                targets = state["validation_targets"]
                if np.any(targets < limits[:, 0]) or np.any(targets > limits[:, 1]):
                    self._finish_calibration("Validation target outside safe limits")
                    return
                duration = state["validation_duration"]
                self._send_targets(targets, duration)
                state["controller"].ready_at = now + duration + state["candidate"].settle_s
                state["phase"] = "validate"
            return
        target = state["plan"][state["index"]]
        if np.any(target < limits[:, 0]) or np.any(target > limits[:, 1]):
            self._finish_calibration("Calibration target outside current safe limits")
            return
        if state["phase"] == "announce":
            token = f"calibration-{time.monotonic_ns()}"
            state.update(phase="speech", token=token, deadline=now + 45)
            angles = " and ".join(f"{axis} {angle:.1f} degrees"
                                  for axis, angle in zip(("pan", "tilt"), target))
            self.bus.publish("av.say", {
                "text": f"Calibration. Keep still and clear of the head. Moving {angles}.",
                "request_id": token,
            })
            return
        if state["phase"] == "speech":
            spoke = self.bus.last("av.spoke")
            if not isinstance(spoke, dict) or spoke.get("request_id") != state["token"]:
                return
            distance = float(np.max(np.abs(target - positions)))
            cfg = self._visual_cfg
            duration = max(
                1.0, 1.875 * distance / cfg.max_speed_deg_s,
                math.sqrt(5.78 * distance / cfg.max_accel_deg_s2),
            )
            self._send_targets(target, duration)
            state.update(
                phase="observe", moved=now, ready=now + duration + cfg.settle_s,
                deadline=now + cfg.calibration_timeout_s, samples=[],
            )
            return
        error, stamp, _, _ = observation
        if stamp <= state["last_stamp"] or stamp < state["ready"]:
            return
        state["last_stamp"] = stamp
        if np.max(np.abs(positions - target)) > 0.15:
            self._finish_calibration("Commanded position not reached; model not saved")
            return
        state["samples"].append(error.copy())
        if len(state["samples"]) < self._visual_cfg.calibration_samples:
            return
        samples = np.asarray(state["samples"])
        noise = float(np.max(np.std(samples, axis=0)))
        if noise > 0.01:
            self._finish_calibration("Target moved or detections too noisy; model not saved")
            return
        state["commands"].append((target - state["origin"]).tolist())
        state["points"].append(np.median(samples, axis=0).tolist())
        # Include observation cadence/inference delay in the conservative
        # response budget; this is not a physical shaft-delay measurement.
        state["durations"].append(stamp - state["moved"])
        state["noise"].append(noise)
        state["index"] += 1
        self.bus.publish("tracking.autotune_progress", {
            "phase": "calibration", "progress": state["index"] / len(state["plan"]),
            "message": f"Validated probe {state['index']} of {len(state['plan'])}",
        })
        if state["index"] < len(state["plan"]):
            state.update(phase="announce", deadline=now + 45)
            return
        try:
            model = identify_plant(
                np.asarray(state["commands"]), np.asarray(state["points"]),
                state["durations"], max(state["noise"]), state["signature"],
            )
        except (ValueError, OSError) as exc:
            self._finish_calibration(str(exc))
            return
        state.update(
            phase="validate", candidate=model,
            controller=VisualController(self._visual_cfg),
            initial_error=float(np.max(np.abs(observation[0][:len(positions)]))),
            centered=0, deadline=now + self._visual_cfg.validation_timeout_s,
        )
        self.bus.publish("tracking.autotune_progress", {
            "stage": "validate", "msg": "Checking live centering before saving calibration",
        })

    def _validate_calibration(self, now, observation, positions, limits):
        state = self._calibration
        if state is None:
            return
        error, stamp, _, _ = observation
        controller = state["controller"]
        if stamp <= controller.last_stamp or stamp < controller.ready_at:
            return
        model = state["candidate"]
        result = controller.correction(error, positions, limits, stamp, now, model)
        if result is not None:
            targets, duration = result
            token = f"validation-{time.monotonic_ns()}"
            # Validation corrections follow the same announced-move rule as
            # identification probes. Keep their observation for the speech phase.
            state.update(phase="validation_speech", token=token,
                         validation_targets=targets, validation_duration=duration)
            self.bus.publish("av.say", {
                "text": "Calibration validation. Making a small centering correction. Keep still.",
                "request_id": token,
            })
            return
        norm = float(np.max(np.abs(error[:len(positions)])))
        if norm <= self._visual_cfg.stop_error:
            state["centered"] += 1
        else:
            state["centered"] = 0
        if controller.state == "at_limit":
            self._finish_calibration("Target not reachable within servo limits; model not saved")
            return
        if state["centered"] >= self._visual_cfg.calibration_samples:
            if norm > state["initial_error"] + model.noise:
                self._finish_calibration("Validation did not improve centering; model not saved")
                return
            try:
                model.save(self._calibration_path())
            except OSError as exc:
                self._finish_calibration(f"Could not save calibration: {exc}")
                return
            self._plant = model
            self._calibration_signature = state["signature"]
            self._finish_calibration()


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
