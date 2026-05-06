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

Topics published
----------------
motion.pan_to       ``{angle: float, move_time_ms: float}``
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
    ) -> None:
        super().__init__(bus=bus)
        self._tracker_cfg = config or HeadTrackerConfig()
        self._enabled = enabled
        self._tracker: Optional[HeadTracker] = None
        self._current_face_cx: Optional[float] = None
        self._current_servo_angle: Optional[float] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubs: list = []

    def on_start(self) -> None:
        if not self._enabled:
            log.info("TrackingService disabled via config")
            return

        self._tracker = HeadTracker(
            initial_servo_angle=180.0,
            config=self._tracker_cfg,
        )

        self._unsubs.append(
            self.bus.subscribe("perception.faces", self._on_faces)
        )
        self._unsubs.append(
            self.bus.subscribe("motion.position", self._on_position)
        )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="head-tracker",
            daemon=True,
        )
        self._thread.start()
        log.info("TrackingService started (%d Hz, enabled=%s)", _UPDATE_HZ, self._enabled)

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

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        if faces:
            # Primary face: highest confidence
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
                face_cx = self._current_face_cx
                servo_angle = self._current_servo_angle

            if self._tracker is None:
                time.sleep(_UPDATE_INTERVAL)
                continue

            target = self._tracker.update(
                face_cx=face_cx,
                dt=dt,
                current_servo_angle=servo_angle,
            )

            # Publish pan_to with a move_time window equal to our update interval.
            # This tells the servo controller how fast to get there — smooth,
            # not instantaneous — so physical movement matches the tracker speed.
            move_ms = _UPDATE_INTERVAL * 1000.0 * 2.0   # 100 ms lookahead
            self.bus.publish("motion.pan_to", {
                "angle": round(target, 2),
                "move_time_ms": round(move_ms, 1),
            })

            elapsed = time.monotonic() - now
            sleep_t = max(0.001, _UPDATE_INTERVAL - elapsed)
            time.sleep(sleep_t)
