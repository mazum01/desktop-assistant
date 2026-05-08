"""
Head tracking service.

Subscribes to ``perception.faces``, selects the primary face (highest
confidence), feeds it to ``HeadTracker``, and publishes ``motion.pan_to``
at 20 Hz so the servo follows with natural spring-damper motion.

When no face is visible for more than *idle_timeout_s* the tracker enters
idle mode and produces slow, human-like gaze drifts.

Topics subscribed
-----------------
perception.faces    ``{faces: [{centroid, confidence, …}], …}``
motion.position     ``{angle: float}``  — servo position feedback
tracking.set_face_tracking   ``{"enabled": bool}``
tracking.set_random_motion   ``{"enabled": bool}``

Topics published
----------------
motion.pan_to       ``{angle: float, move_time_ms: float}``
tracking.face_tracking_changed   ``{"enabled": bool}``
tracking.random_motion_changed   ``{"enabled": bool}``
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.motion.head_tracker import HeadTracker, HeadTrackerConfig

log = logging.getLogger(__name__)

_UPDATE_HZ = 20
_UPDATE_INTERVAL = 1.0 / _UPDATE_HZ


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
    ) -> None:
        super().__init__(bus=bus)
        self._tracker_cfg = config or HeadTrackerConfig()
        self._enabled = enabled
        self._face_tracking_enabled = face_tracking_enabled
        self._random_motion_enabled = random_motion_enabled
        self._tracker: Optional[HeadTracker] = None
        self._current_face_cx: Optional[float] = None
        self._current_servo_angle: Optional[float] = None
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

    def on_start(self) -> None:
        if not self._enabled:
            log.info("TrackingService disabled via config")
            return

        self._tracker = HeadTracker(
            initial_servo_angle=180.0,
            config=self._tracker_cfg,
        )

        self._unsubs.append(self.bus.subscribe("perception.faces", self._on_faces))
        self._unsubs.append(self.bus.subscribe("motion.position", self._on_position))
        self._unsubs.append(self.bus.subscribe("tracking.set_face_tracking", self._on_set_face_tracking))
        self._unsubs.append(self.bus.subscribe("tracking.set_random_motion", self._on_set_random_motion))

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="head-tracker",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "TrackingService started (%d Hz, enabled=%s, face_tracking=%s, random_motion=%s)",
            _UPDATE_HZ, self._enabled, self._face_tracking_enabled, self._random_motion_enabled,
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

    def _on_position(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._lock:
                self._current_servo_angle = float(payload["angle"])

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

            if self._tracker is None:
                time.sleep(_UPDATE_INTERVAL)
                continue

            # If random motion is disabled and there's no face to track, hold position.
            if not self._random_motion_enabled and face_cx is None:
                time.sleep(_UPDATE_INTERVAL)
                continue

            target = self._tracker.update(
                face_cx=face_cx,
                dt=dt,
                current_servo_angle=servo_angle,
            )

            move_ms = _UPDATE_INTERVAL * 1000.0 * 2.0
            self.bus.publish("motion.pan_to", {
                "angle": round(target, 2),
                "move_time_ms": round(move_ms, 1),
            })

            elapsed = time.monotonic() - now
            sleep_t = max(0.001, _UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_t)
