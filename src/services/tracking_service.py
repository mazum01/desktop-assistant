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
import threading
import time
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.motion.head_tracker import HeadTracker, HeadTrackerConfig

log = logging.getLogger(__name__)

_UPDATE_HZ = 20
_UPDATE_INTERVAL = 1.0 / _UPDATE_HZ

# Maximum age (seconds) of a person detection hint before it is discarded.
# At 2 fps object detection this equals ~4 missed frames.
_PERSON_SEEK_STALE_S = 2.0


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

            elapsed = time.monotonic() - now
            sleep_t = max(0.001, _UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_t)
