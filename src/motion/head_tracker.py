"""
Natural head tracking — Kalman filter + minimum-jerk trajectory controller.

The tracker maintains a position/velocity state and updates at a fixed rate
(typically 20 Hz).  Callers feed it the current face centroid X in pixels;
the tracker returns the recommended servo angle each tick.

Motion model
------------
Face centroid is smoothed by a 1-D Kalman filter [position, velocity] that
predicts where the face will be a short look-ahead time ahead.  The desired
servo angle is computed from the Kalman-predicted face position.

Servo motion follows a **minimum-jerk** (5th-order polynomial) trajectory
(Flash & Hogan 1985) — the same profile used by human arm and head movements.
The velocity profile is a smooth bell curve: gentle acceleration → peak speed
at mid-point → smooth deceleration to rest.  The planner replans from the
current (position, velocity) whenever the target shifts significantly,
preserving momentum so direction changes look natural rather than abrupt.

Idle behaviour (no face)
------------------------
The same min-jerk planner drives idle sweeps so bored gaze movements share
the same smooth quality.  The tracker picks random angles, pauses, and drifts
with tiny micro-noise between sweeps.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional, Tuple

import numpy as np

from src.motion.face_kalman import FaceKalman

log = logging.getLogger(__name__)


# ── Minimum-jerk trajectory planner ─────────────────────────────────────────

class _MinJerkPlanner:
    """5th-order minimum-jerk trajectory planner.

    Plans a smooth point-to-point move from (x0, v0) to (xf, 0) in time T.
    Replanning mid-trajectory preserves momentum: the new polynomial starts
    from the current (position, velocity) so there is no discontinuity.

    Boundary conditions: x(0)=x0, v(0)=v0, a(0)=0, x(T)=xf, v(T)=0, a(T)=0
    Polynomial form: x(t) = x0 + v0·t + c3·t³ + c4·t⁴ + c5·t⁵
    """

    def __init__(self) -> None:
        self._x0 = self._v0 = self._xf = 0.0
        self._c3 = self._c4 = self._c5 = 0.0
        self._T: float = 1.0
        self._t: float = 0.0

    def plan(self, x0: float, v0: float, xf: float, T: float) -> None:
        """Compute coefficients for the move (x0, v0) → (xf, 0) in T seconds."""
        T = max(T, 0.02)
        self._x0, self._v0, self._xf = float(x0), float(v0), float(xf)
        self._T, self._t = T, 0.0

        dx = xf - x0
        T2, T3, T4, T5 = T ** 2, T ** 3, T ** 4, T ** 5
        A = np.array([
            [T3,    T4,     T5   ],
            [3*T2,  4*T3,   5*T4 ],
            [6*T,   12*T2,  20*T3],
        ])
        b = np.array([dx - v0 * T, -v0, 0.0])
        try:
            c3, c4, c5 = np.linalg.solve(A, b)
        except np.linalg.LinAlgError:
            c3 = c4 = c5 = 0.0
        self._c3, self._c4, self._c5 = float(c3), float(c4), float(c5)

    def step(self, dt: float) -> tuple[float, float]:
        """Advance by *dt* seconds; return (position, velocity) on the curve."""
        self._t = min(self._t + dt, self._T)
        t = self._t
        pos = (self._x0 + self._v0 * t
               + self._c3 * t ** 3 + self._c4 * t ** 4 + self._c5 * t ** 5)
        vel = (self._v0
               + 3 * self._c3 * t ** 2 + 4 * self._c4 * t ** 3 + 5 * self._c5 * t ** 4)
        return float(pos), float(vel)

    @property
    def done(self) -> bool:
        return self._t >= self._T

    @property
    def target(self) -> float:
        return self._xf


# ── Config ───────────────────────────────────────────────────────────────────

@dataclass
class HeadTrackerConfig:
    # Frame geometry
    frame_width: int = 1280
    fov_degrees: float = 100.0

    # Dead zone — fraction of frame width ignored before tracking activates
    dead_zone_frac: float = 0.06

    # Gain: fraction of Kalman-predicted face offset used as servo target.
    # 1.0 = fully centre the face; lower values leave some residual offset.
    tracking_gain: float = 0.92

    # Direction flip if servo mount reverses left/right
    invert_pan: bool = False

    # Kalman filter for face centroid
    kalman_r: float = 400.0      # measurement noise variance (px²)
    kalman_q_pos: float = 1.0    # process noise — position
    kalman_q_vel: float = 50.0   # process noise — velocity
    lookahead_s: float = 0.05    # predict face this far ahead (seconds)

    # Minimum-jerk trajectory parameters
    replan_threshold_deg: float = 2.0   # replan when target shifts by this many °
    move_base_s: float = 0.15           # minimum trajectory duration (s)
    move_scale_s_per_deg: float = 0.005 # extra duration per degree of travel
    move_max_s: float = 0.55            # cap on trajectory duration (s)
    max_speed_deg_s: float = 250.0      # hard velocity cap (servo physical limit)

    # Micro-saccades when locked on face (natural small fixation movements)
    saccade_amplitude_deg: float = 1.5
    saccade_interval_s: Tuple[float, float] = (3.0, 7.0)

    # Idle gaze parameters
    idle_speed_deg_s: float = 22.0
    idle_pause_s: Tuple[float, float] = (2.0, 8.0)
    idle_range_deg: Tuple[float, float] = (90.0, 270.0)

    # Servo limits (logical 1–360)
    servo_min: float = 1.0
    servo_max: float = 360.0
    servo_center: float = 180.0


# Whitelist of HeadTrackerConfig fields that may be tuned live from the web UI
TUNABLE_FIELDS: dict[str, tuple[float, float]] = {
    # name → (min, max) allowed range
    "tracking_gain":          (0.05, 1.00),
    "dead_zone_frac":         (0.00, 0.20),
    "max_speed_deg_s":        (20.0, 350.0),
    "kalman_r":               (50.0, 2000.0),
    "kalman_q_pos":           (0.0, 50.0),
    "kalman_q_vel":           (1.0, 500.0),
    "lookahead_s":            (0.0, 0.20),
    "replan_threshold_deg":   (0.3, 12.0),
    "move_base_s":            (0.05, 0.60),
    "move_scale_s_per_deg":   (0.001, 0.050),
    "move_max_s":             (0.20, 2.00),
}


# Named presets covering only the most expressive params; remaining values
# fall back to whatever is currently set when a preset is applied.
PRESETS: dict[str, dict[str, float]] = {
    "default": {
        "tracking_gain": 0.55,
        "max_speed_deg_s": 150.0,
        "kalman_r": 400.0,
        "kalman_q_vel": 50.0,
        "lookahead_s": 0.02,
        "replan_threshold_deg": 2.0,
        "move_base_s": 0.18,
        "move_scale_s_per_deg": 0.012,
        "move_max_s": 0.60,
        "dead_zone_frac": 0.06,
    },
    "snappy": {
        "tracking_gain": 0.45,
        "max_speed_deg_s": 200.0,
        "kalman_r": 300.0,
        "kalman_q_vel": 80.0,
        "lookahead_s": 0.04,
        "replan_threshold_deg": 1.5,
        "move_base_s": 0.12,
        "move_scale_s_per_deg": 0.008,
        "move_max_s": 0.45,
        "dead_zone_frac": 0.05,
    },
    "smooth": {
        "tracking_gain": 0.25,
        "max_speed_deg_s": 90.0,
        "kalman_r": 700.0,
        "kalman_q_vel": 30.0,
        "lookahead_s": 0.0,
        "replan_threshold_deg": 3.5,
        "move_base_s": 0.30,
        "move_scale_s_per_deg": 0.020,
        "move_max_s": 1.00,
        "dead_zone_frac": 0.08,
    },
}


class _IdleState(Enum):
    MOVING = auto()
    PAUSING = auto()


# ── HeadTracker ──────────────────────────────────────────────────────────────

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
        self._pos: float = float(initial_servo_angle)
        self._vel: float = 0.0

        # Motion planner — shared between tracking and idle modes
        self._planner = _MinJerkPlanner()
        self._planner.plan(self._pos, 0.0, self._pos, 0.1)  # trivial no-op plan

        # Kalman face filter — reset when face is lost
        self._kalman = FaceKalman(
            r=self._cfg.kalman_r,
            q_pos=self._cfg.kalman_q_pos,
            q_vel=self._cfg.kalman_q_vel,
        )

        # Micro-saccade state
        self._saccade_offset: float = 0.0
        self._next_saccade_t: float = time.monotonic() + self._next_saccade_interval()

        # Idle state machine
        self._idle_state = _IdleState.PAUSING
        self._idle_target: float = self._cfg.servo_center
        self._idle_pause_until: float = time.monotonic()
        self._last_face_t: float = 0.0

        # Track whether the previous tick was in tracking mode (for smooth handoff)
        self._was_tracking: bool = False

        # ── Debug / live-tuning state ─────────────────────────────────
        # Last frame's raw + smoothed face X and Kalman velocity estimate, plus
        # the trajectory target.  Read by TrackingService for the live UI chart.
        self._dbg_face_raw: Optional[float] = None
        self._dbg_face_smoothed: Optional[float] = None
        self._dbg_face_vel: float = 0.0
        self._dbg_target: float = self._pos
        self._dbg_mode: str = "idle"

    # ── Live-tuning API ──────────────────────────────────────────────────

    def update_config(self, name: str, value: float) -> bool:
        """Mutate a single tunable config field at runtime.

        Returns True if the field was applied, False if unknown / out of range.
        """
        if name not in TUNABLE_FIELDS:
            return False
        lo, hi = TUNABLE_FIELDS[name]
        v = float(value)
        if not (lo <= v <= hi):
            log.warning("update_config: %s=%.4f outside [%.4f, %.4f]", name, v, lo, hi)
            return False
        setattr(self._cfg, name, v)
        # Kalman noise params need to propagate into the filter object.
        if name == "kalman_r":
            self._kalman.r = v
        elif name == "kalman_q_pos":
            self._kalman.q_pos = v
        elif name == "kalman_q_vel":
            self._kalman.q_vel = v
        return True

    def get_config(self) -> dict:
        """Return current values of all tunable parameters."""
        return {k: float(getattr(self._cfg, k)) for k in TUNABLE_FIELDS.keys()}

    def get_debug_state(self) -> dict:
        """Return a snapshot of internal tracker state for the live UI."""
        return {
            "face_raw":      self._dbg_face_raw,
            "face_smoothed": self._dbg_face_smoothed,
            "face_vel":      self._dbg_face_vel,
            "servo_angle":   self._pos,
            "target":        self._dbg_target,
            "mode":          self._dbg_mode,
        }

    # ── Public API ───────────────────────────────────────────────────────

    @property
    def servo_angle(self) -> float:
        """Current recommended servo angle (degrees, clamped to servo range)."""
        return self._clamp(self._pos)

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
            self._pos = float(current_servo_angle)

        currently_tracking = face_cx is not None

        if self._was_tracking and not currently_tracking:
            # Face just lost — plan a smooth deceleration to a natural stop
            self._kalman.reset()
            decel_s = max(0.12, abs(self._vel) / 200.0)
            stop_target = self._clamp(self._pos + self._vel * decel_s * 0.5)
            self._planner.plan(self._pos, self._vel, stop_target, decel_s)
            self._idle_state = _IdleState.PAUSING
            self._idle_pause_until = (time.monotonic()
                                      + random.uniform(*self._cfg.idle_pause_s))

        self._was_tracking = currently_tracking

        if currently_tracking:
            return self._update_tracking(face_cx, dt)  # type: ignore[arg-type]
        return self._update_idle(dt)

    # ── Tracking mode ────────────────────────────────────────────────────

    def _update_tracking(self, face_cx: float, dt: float) -> float:
        self._last_face_t = time.monotonic()
        cfg = self._cfg
        self._dbg_face_raw = float(face_cx)
        self._dbg_mode = "tracking"

        # Step 1: Kalman filter — smooth detection noise and estimate velocity
        smooth_cx, face_vel = self._kalman.update(face_cx, dt)
        pred_cx = self._kalman.predict(cfg.lookahead_s) or smooth_cx
        self._dbg_face_smoothed = float(smooth_cx)
        self._dbg_face_vel = float(face_vel)

        # Step 2: Convert predicted face X → desired servo angle
        offset_frac = (pred_cx - cfg.frame_width / 2.0) / cfg.frame_width
        offset_deg = offset_frac * cfg.fov_degrees

        dead_deg = cfg.dead_zone_frac * cfg.fov_degrees
        if abs(offset_deg) < dead_deg:
            offset_deg = 0.0

        direction = -1.0 if cfg.invert_pan else 1.0
        desired_target = self._pos + direction * offset_deg * cfg.tracking_gain
        desired_target = self._clamp(desired_target)

        # Step 3: Replan only when the target has shifted enough to matter.
        # This prevents constant replanning from residual Kalman noise.
        if abs(desired_target - self._planner.target) > cfg.replan_threshold_deg:
            dist = abs(desired_target - self._pos)
            T = min(cfg.move_base_s + cfg.move_scale_s_per_deg * dist, cfg.move_max_s)
            self._planner.plan(self._pos, self._vel, desired_target, T)
        self._dbg_target = self._planner.target

        # Step 4: Advance the min-jerk trajectory
        pos, vel = self._planner.step(dt)
        vel = float(np.clip(vel, -cfg.max_speed_deg_s, cfg.max_speed_deg_s))
        self._pos = pos
        self._vel = vel

        # Step 5: Micro-saccades (natural fixation micro-movements)
        now = time.monotonic()
        if now >= self._next_saccade_t:
            self._saccade_offset = random.gauss(0, cfg.saccade_amplitude_deg * 0.5)
            self._next_saccade_t = now + self._next_saccade_interval()

        return self._clamp(pos + self._saccade_offset)

    # ── Idle mode ────────────────────────────────────────────────────────

    def _update_idle(self, dt: float) -> float:
        now = time.monotonic()
        cfg = self._cfg
        self._dbg_face_raw = None
        self._dbg_face_smoothed = None
        self._dbg_mode = "idle"

        if self._idle_state == _IdleState.PAUSING:
            # If we're still decelerating from a tracking move, step the planner
            if not self._planner.done:
                pos, vel = self._planner.step(dt)
                self._pos = pos
                self._vel = vel
                return self._clamp(pos)

            # Settled — tiny micro-drift while paused (breathing-like)
            self._vel = 0.0
            if now >= self._idle_pause_until:
                target = self._pick_idle_target()
                dist = abs(target - self._pos)
                # Duration gives natural-feeling idle speed via min-jerk peak
                T = max(cfg.move_base_s,
                        1.875 * dist / max(cfg.idle_speed_deg_s, 1.0))
                T = min(T, 6.0)
                self._planner.plan(self._pos, 0.0, target, T)
                self._idle_state = _IdleState.MOVING
            else:
                self._pos += random.gauss(0, 0.05)
                return self._clamp(self._pos)

        # MOVING — step the min-jerk trajectory
        pos, vel = self._planner.step(dt)
        self._pos = pos
        self._vel = vel

        if self._planner.done:
            self._vel = 0.0
            self._idle_pause_until = now + random.uniform(*cfg.idle_pause_s)
            self._idle_state = _IdleState.PAUSING

        return self._clamp(self._pos)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _pick_idle_target(self) -> float:
        """Pick a random gaze target that isn't too close to current position."""
        lo, hi = self._cfg.idle_range_deg
        for _ in range(10):
            t = random.uniform(lo, hi)
            if abs(t - self._pos) > 15.0:
                return t
        return random.uniform(lo, hi)

    def _next_saccade_interval(self) -> float:
        lo, hi = self._cfg.saccade_interval_s
        return random.uniform(lo, hi)

    def _clamp(self, angle: float) -> float:
        return float(min(max(angle, self._cfg.servo_min), self._cfg.servo_max))
