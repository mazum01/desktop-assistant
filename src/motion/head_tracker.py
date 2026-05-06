"""
Natural head tracking — spring-damper servo control with organic idle behaviour.

The tracker maintains a simulated position/velocity state and updates at a
fixed rate (typically 20 Hz). Callers feed it the current face centroid X in
pixels and retrieve the recommended servo angle via ``servo_angle``.

Spring-damper model
-------------------
  velocity += (target − position) * spring_k  − velocity * damping
  position += velocity * dt

This gives a slightly under-damped response: the head smoothly accelerates
toward the face, overshoots slightly, and settles — much like a real person.

Idle behaviour (no face)
------------------------
The tracker drifts through a random-gaze state machine rather than snapping
to centre.  It chooses a random angle within a comfortable arc, moves toward
it at "bored" speed, pauses for a random interval, then picks the next target.
Occasional slow sweeps and brief holds reproduce natural inattentive scanning.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class HeadTrackerConfig:
    # Frame geometry
    frame_width: int = 1280
    fov_degrees: float = 100.0   # camera horizontal field of view

    # Spring-damper tracking
    spring_k: float = 6.0        # stiffness — higher = faster tracking
    damping: float = 2.5         # underdamped: 2*sqrt(6)≈4.9 is critical
    max_speed_deg_s: float = 80.0

    # Dead zone — fraction of frame width before the head starts following
    dead_zone_frac: float = 0.04

    # Micro-saccades when locked on face (simulates natural small eye/head movements)
    saccade_amplitude_deg: float = 1.5
    saccade_interval_s: Tuple[float, float] = (3.0, 7.0)

    # Idle gaze parameters
    idle_speed_deg_s: float = 22.0          # slow, bored
    idle_pause_s: Tuple[float, float] = (2.0, 8.0)   # pause range at each position
    idle_range_deg: Tuple[float, float] = (90.0, 270.0)  # comfortable arc to scan

    # Servo limits (logical 1–360)
    servo_min: float = 1.0
    servo_max: float = 360.0
    servo_center: float = 180.0


class _IdleState(Enum):
    MOVING = auto()
    PAUSING = auto()


class HeadTracker:
    """Stateful head-tracking controller.

    Update loop (call at fixed rate)::

        tracker = HeadTracker(initial_servo_angle=180.0)
        while True:
            angle = tracker.update(face_cx=640, dt=0.05)  # face in frame
            angle = tracker.update(face_cx=None, dt=0.05)  # no face → idle
            servo.move_to(angle)
    """

    def __init__(
        self,
        initial_servo_angle: float = 180.0,
        config: Optional[HeadTrackerConfig] = None,
    ) -> None:
        self._cfg = config or HeadTrackerConfig()
        self._position: float = initial_servo_angle   # current servo angle (deg)
        self._velocity: float = 0.0                   # deg/sec

        # Micro-saccade state
        self._saccade_offset: float = 0.0
        self._next_saccade_t: float = time.monotonic() + self._next_saccade_interval()

        # Idle state machine
        self._idle_state = _IdleState.PAUSING
        self._idle_target: float = self._cfg.servo_center
        self._idle_pause_until: float = time.monotonic()
        self._last_face_t: float = 0.0   # last time a face was seen

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def servo_angle(self) -> float:
        """Current recommended servo angle (degrees, clamped to servo range)."""
        return self._clamp(self._position)

    def update(
        self,
        face_cx: Optional[float],
        dt: float,
        current_servo_angle: Optional[float] = None,
    ) -> float:
        """Advance the tracker by *dt* seconds.

        Parameters
        ----------
        face_cx:
            X pixel coordinate of the primary face centroid, or ``None``
            when no face is visible.
        dt:
            Time elapsed since last update (seconds).
        current_servo_angle:
            Actual servo position if available (used to sync state after
            external moves). Pass ``None`` to skip sync.

        Returns
        -------
        float
            Recommended servo target angle (degrees, clamped to valid range).
        """
        if current_servo_angle is not None:
            self._position = float(current_servo_angle)

        if face_cx is not None:
            return self._update_tracking(face_cx, dt)
        return self._update_idle(dt)

    # ── Tracking mode ────────────────────────────────────────────────────

    def _update_tracking(self, face_cx: float, dt: float) -> float:
        self._last_face_t = time.monotonic()

        # Map face X → desired servo angle offset from current position
        cfg = self._cfg
        offset_frac = (face_cx - cfg.frame_width / 2.0) / cfg.frame_width
        offset_deg = offset_frac * cfg.fov_degrees

        # Dead zone — ignore tiny movements
        dead_deg = cfg.dead_zone_frac * cfg.fov_degrees
        if abs(offset_deg) < dead_deg:
            offset_deg = 0.0

        # Target: move servo so face is centred (invert: servo moves camera)
        target = self._position - offset_deg

        # Micro-saccades
        now = time.monotonic()
        if now >= self._next_saccade_t:
            self._saccade_offset = random.gauss(0, cfg.saccade_amplitude_deg * 0.5)
            self._next_saccade_t = now + self._next_saccade_interval()
        target += self._saccade_offset

        # Spring-damper
        accel = (target - self._position) * cfg.spring_k - self._velocity * cfg.damping
        self._velocity += accel * dt
        # Cap speed
        self._velocity = np.clip(self._velocity, -cfg.max_speed_deg_s, cfg.max_speed_deg_s)
        self._position += self._velocity * dt

        return self._clamp(self._position)

    # ── Idle mode ────────────────────────────────────────────────────────

    def _update_idle(self, dt: float) -> float:
        now = time.monotonic()
        cfg = self._cfg

        # Gradually damp velocity so we don't keep drifting after losing a face
        self._velocity *= max(0.0, 1.0 - dt * 3.0)

        if self._idle_state == _IdleState.PAUSING:
            if now >= self._idle_pause_until:
                self._idle_target = self._pick_idle_target()
                self._idle_state = _IdleState.MOVING
            else:
                # Small random noise during pause (breathing-like micro-drift)
                self._position += random.gauss(0, 0.08)
                return self._clamp(self._position)

        # MOVING
        diff = self._idle_target - self._position
        if abs(diff) < 1.0:
            # Arrived — pause
            self._position = self._idle_target
            self._velocity = 0.0
            pause = random.uniform(*cfg.idle_pause_s)
            self._idle_pause_until = now + pause
            self._idle_state = _IdleState.PAUSING
        else:
            step = cfg.idle_speed_deg_s * dt
            move = np.sign(diff) * min(abs(diff), step)
            self._position += move

        return self._clamp(self._position)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _pick_idle_target(self) -> float:
        """Pick a random gaze target that isn't too close to the current position."""
        lo, hi = self._cfg.idle_range_deg
        for _ in range(10):
            t = random.uniform(lo, hi)
            if abs(t - self._position) > 15.0:  # ensure some movement
                return t
        return random.uniform(lo, hi)

    def _next_saccade_interval(self) -> float:
        lo, hi = self._cfg.saccade_interval_s
        return random.uniform(lo, hi)

    def _clamp(self, angle: float) -> float:
        return float(np.clip(angle, self._cfg.servo_min, self._cfg.servo_max))
