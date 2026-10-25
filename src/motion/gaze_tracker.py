"""
Person-like gaze tracking: world-frame Kalman filtering + minimum-jerk motion.

Each fresh detection is converted to an *absolute bearing*: the head angle at
the moment the frame was captured plus the target's angular offset in the
image.  Filtering that bearing (instead of the pixel offset) means a cached or
late detection can never be re-applied relative to where the head has since
moved, so the controller cannot accumulate overshoot or hunt.

Motion mimics human head/gaze behaviour:

* **Saccade-like re-fixation** — larger shifts are a single minimum-jerk move
  whose duration grows with amplitude (the "main sequence"), giving a
  bell-shaped velocity profile.
* **Smooth pursuit** — when the Kalman filter sees the target moving, moves
  aim where the target will be on arrival and finish at the target's velocity,
  so the head glides along instead of stopping and starting.
* **Fixation hold** — once on target the head rests until the target leaves an
  elliptical release zone, rather than chasing detector jitter.
* **Coordinated axes** — pan and tilt replan together with one shared
  duration, so diagonal shifts travel in a straight line instead of a
  staircase.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from dataclasses import asdict, dataclass
from typing import Optional

from src.motion.face_kalman import FaceKalman
from src.motion.head_tracker import HeadTrackerConfig, MinJerkPlanner


@dataclass
class GazeConfig:
    # Servo output trails the commanded position by roughly this long.
    servo_lag_s: float = 0.06
    # Never extrapolate a target further than this past its last detection.
    max_predict_s: float = 0.4
    # Below this filtered speed the target is treated as stationary.
    pursuit_min_deg_s: float = 3.0
    # Upper bound on the target velocity the head will try to match.
    max_pursuit_deg_s: float = 60.0
    # A detection this far from the prediction is a new target, not motion.
    reacquire_jump_deg: float = 15.0
    # How long to keep coasting on prediction once detections stop.
    lost_s: float = 1.0
    # Uncertainty in when a frame was captured relative to the head-angle
    # history; bearings taken while the head moves are trusted less.
    timing_uncertainty_s: float = 0.1
    # Saccadic suppression: ignore frames captured while the head moves faster
    # than this and faster than the target itself (fast re-fixation blur).
    saccade_suppress_deg_s: float = 25.0
    # Errors above this are a re-fixation (no velocity matching) that lands at
    # saccade_gain of the distance; smaller errors use pursuit/corrections.
    saccade_min_deg: float = 8.0
    saccade_gain: float = 0.9
    # Elliptical zones (pan, tilt half-axes in degrees). Corrections stop once
    # the error is inside the settle zone; after fixation locks, the target
    # must leave the larger release zone before the head moves again.
    settle_pan_deg: float = 3.0
    settle_tilt_deg: float = 2.0
    release_pan_deg: float = 6.0
    release_tilt_deg: float = 4.0
    # Pursuit is blocked for this long after a saccade lands.
    pursuit_settle_s: float = 0.25
    # Pursuit needs |velocity| above this many filter standard deviations.
    pursuit_confidence: float = 2.0
    # Error must stay inside the dead zone this long before fixation locks.
    fixation_dwell_s: float = 0.3

    def __post_init__(self):
        values = asdict(self)
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   and math.isfinite(v) for v in values.values()):
            raise ValueError("Gaze settings must be finite numbers")
        if not 0 <= self.servo_lag_s <= 0.5:
            raise ValueError("servo_lag_s must be within 0-0.5 s")
        if not 0 < self.max_predict_s <= 1.0:
            raise ValueError("max_predict_s must be within 0-1 s")
        if not 0 <= self.pursuit_min_deg_s < self.max_pursuit_deg_s <= 120:
            raise ValueError("Require 0 <= pursuit_min < max_pursuit <= 120 deg/s")
        if not 1 <= self.reacquire_jump_deg <= 90:
            raise ValueError("reacquire_jump_deg must be within 1-90 degrees")
        if not 0 < self.lost_s <= 5:
            raise ValueError("lost_s must be within 0-5 s")
        if not 0 <= self.timing_uncertainty_s <= 0.5:
            raise ValueError("timing_uncertainty_s must be within 0-0.5 s")
        if not 5 <= self.saccade_suppress_deg_s <= 200:
            raise ValueError("saccade_suppress_deg_s must be within 5-200 deg/s")
        if not 2 <= self.saccade_min_deg <= 45:
            raise ValueError("saccade_min_deg must be within 2-45 degrees")
        if not 0.5 <= self.saccade_gain <= 1.0:
            raise ValueError("saccade_gain must be within 0.5-1.0")
        if not (0.5 <= self.settle_pan_deg <= self.release_pan_deg <= 20
                and 0.5 <= self.settle_tilt_deg <= self.release_tilt_deg <= 20):
            raise ValueError("Require 0.5 <= settle <= release <= 20 degrees per axis")
        if not 0 <= self.pursuit_settle_s <= 2:
            raise ValueError("pursuit_settle_s must be within 0-2 seconds")
        if not 0 <= self.pursuit_confidence <= 5:
            raise ValueError("pursuit_confidence must be within 0-5")
        if not 0 <= self.fixation_dwell_s <= 3:
            raise ValueError("fixation_dwell_s must be within 0-3 seconds")


def image_offsets(point, cfg: HeadTrackerConfig) -> tuple[float, float]:
    """Return (pan, tilt) degrees needed to center a normalized image point.

    ``point`` is ``(x, y)`` in ``[-0.5, 0.5]`` with +y down.  Tilt follows the
    logical convention where a higher angle looks up.
    """
    half = math.tan(math.radians(cfg.fov_degrees) / 2)
    vertical_half = math.tan(math.radians(cfg.vertical_fov()) / 2)
    direction = -1.0 if cfg.invert_pan else 1.0
    pan = direction * math.degrees(math.atan(2 * float(point[0]) * half))
    tilt = math.degrees(math.atan(-2 * float(point[1]) * vertical_half))
    return pan, tilt


class AngleHistory:
    """Recent measured head angles, interpolated at a past timestamp."""

    def __init__(self, maxlen: int = 64):
        self._samples: deque = deque(maxlen=maxlen)

    def add(self, t: float, pan: float, tilt: Optional[float]) -> None:
        if self._samples and t < self._samples[-1][0]:
            self._samples.clear()
        self._samples.append((float(t), float(pan), None if tilt is None else float(tilt)))

    def at(self, t: float) -> Optional[tuple[float, Optional[float]]]:
        if not self._samples:
            return None
        samples = list(self._samples)
        if t <= samples[0][0]:
            return samples[0][1], samples[0][2]
        if t >= samples[-1][0]:
            return samples[-1][1], samples[-1][2]
        i = bisect_left([s[0] for s in samples], t)
        (t0, p0, k0), (t1, p1, k1) = samples[i - 1], samples[i]
        f = (t - t0) / (t1 - t0) if t1 > t0 else 1.0
        tilt = None if k0 is None or k1 is None else k0 + (k1 - k0) * f
        return p0 + (p1 - p0) * f, tilt

    def speed(self, t: float, span: float = 0.1) -> tuple[float, float]:
        """Return absolute (pan, tilt) angular speeds around time ``t``."""
        before, after = self.at(t - span / 2), self.at(t + span / 2)
        if before is None or after is None:
            return 0.0, 0.0
        tilt = (0.0 if before[1] is None or after[1] is None
                else abs(after[1] - before[1]) / span)
        return abs(after[0] - before[0]) / span, tilt


def _zones(name: str, cfg: HeadTrackerConfig, gaze: GazeConfig) -> tuple[float, float]:
    """Return (settle, release) half-axes in degrees for an axis."""
    if name == "tilt":
        return gaze.settle_tilt_deg, gaze.release_tilt_deg
    settle = max(gaze.settle_pan_deg, cfg.dead_zone_frac * cfg.fov_degrees)
    return settle, max(settle, gaze.release_pan_deg)


class _GazeAxis:
    def __init__(self, position: float, name: str = "pan"):
        self.name = name
        self.kalman = FaceKalman()
        self.planner = MinJerkPlanner()
        self.pos = float(position)
        self.vel = 0.0
        self.acc = 0.0
        self.last_stamp: Optional[float] = None
        self.desired: Optional[float] = None
        self.pursuing = False
        self.fixated = False
        self.goal: Optional[float] = None
        self.settle_s = 0.0
        self.planner.plan(self.pos, 0.0, self.pos, 0.02)
        self.planner.step(1.0)

    def forget(self) -> None:
        self.kalman.reset()
        self.last_stamp = None
        self.pursuing = False
        self.fixated = False

    def observe(self, bearing: float, stamp: float, cfg: HeadTrackerConfig,
                gaze: GazeConfig, head_speed: float = 0.0) -> None:
        if self.last_stamp is not None and stamp <= self.last_stamp:
            return
        if (self.kalman.initialised and head_speed > gaze.saccade_suppress_deg_s
                and head_speed > 1.5 * abs(self.kalman.velocity)):
            return
        # Kalman noise is configured in pixels²; convert to degrees².
        scale = (cfg.fov_degrees / cfg.frame_width) ** 2
        self.kalman.r = (cfg.kalman_r * scale
                         + (head_speed * gaze.timing_uncertainty_s) ** 2)
        self.kalman.q_pos = cfg.kalman_q_pos * scale
        self.kalman.q_vel = cfg.kalman_q_vel * scale
        dt = 0.0
        if self.kalman.initialised and self.last_stamp is not None:
            dt = stamp - self.last_stamp
            if abs(bearing - self.kalman.predict(dt)) > gaze.reacquire_jump_deg:
                self.kalman.reset()
        self.kalman.update(bearing, dt)
        self.last_stamp = float(stamp)

    def estimate(self, now: float, cfg: HeadTrackerConfig,
                 gaze: GazeConfig) -> Optional[tuple[float, float]]:
        if not self.kalman.initialised or self.last_stamp is None:
            return None
        velocity = max(-gaze.max_pursuit_deg_s,
                       min(gaze.max_pursuit_deg_s, self.kalman.velocity))
        # Pursuit hysteresis: start above pursuit_min, keep going down to half.
        threshold = gaze.pursuit_min_deg_s * (0.5 if self.pursuing else 1.0)
        error = abs(self.kalman.position - self.pos)
        # Right after a saccade the velocity estimate is polluted by the head's
        # own motion, so pursuit may not engage until it has settled.
        # A settled fixation only breaks into pursuit once the error is clear.
        # Ignore velocity that is not clearly above the filter's own uncertainty.
        threshold = max(threshold, gaze.pursuit_confidence * (self.kalman.velocity_std or 0.0))
        release = _zones(self.name, cfg, gaze)[1]
        self.pursuing = (abs(velocity) >= threshold and self.settle_s <= 0.0
                         and (not self.fixated or error > release))
        if not self.pursuing:
            velocity = 0.0
        horizon = min(max(0.0, now - self.last_stamp + cfg.lookahead_s), gaze.max_predict_s)
        return self.kalman.position + velocity * horizon, velocity

    def advance(self, dt: float, lo: float, hi: float, cfg: HeadTrackerConfig) -> float:
        pos, vel = self.planner.step(dt)
        acc = self.planner.acc
        limit = cfg.max_speed_deg_s * dt
        if abs(pos - self.pos) > limit:
            pos, acc = self.pos + math.copysign(limit, pos - self.pos), 0.0
        clamped = min(max(pos, lo), hi)
        if clamped != pos:
            vel = acc = 0.0
        self.pos, self.vel, self.acc = clamped, vel, acc
        return self.pos

    def stop(self) -> None:
        """Decelerate smoothly to rest from the current velocity."""
        self.forget()
        decel_s = max(0.15, abs(self.vel) / 150.0)
        self.planner.plan(self.pos, self.vel, self.pos + self.vel * decel_s * 0.5, decel_s,
                          a0=self.acc)
        self.desired = None


class GazeTracker:
    """Multi-axis (pan + optional tilt) person-like target follower."""

    def __init__(self, cfg: HeadTrackerConfig, gaze: Optional[GazeConfig] = None):
        self.cfg = cfg
        self.gaze = gaze or GazeConfig()
        self.axes: dict[str, _GazeAxis] = {}
        self.source: Optional[str] = None
        self.dwell_s = 0.0

    @property
    def active(self) -> bool:
        return self.source is not None

    @property
    def moving(self) -> bool:
        return any(not axis.planner.done for axis in self.axes.values())

    def last_stamp(self) -> float:
        stamps = [a.last_stamp for a in self.axes.values() if a.last_stamp is not None]
        return max(stamps) if stamps else -math.inf

    def engage(self, source: str, positions: dict[str, float]) -> None:
        """Start following ``source``; re-seed from measured angles when idle."""
        if self.source == source:
            return
        if self.source is None and not self.moving:
            self.axes = {name: _GazeAxis(pos, name) for name, pos in positions.items()}
        else:
            for name, pos in positions.items():
                self.axes.setdefault(name, _GazeAxis(pos, name))
            for name in list(self.axes):
                if name not in positions:
                    del self.axes[name]
            for axis in self.axes.values():
                axis.forget()
        self.source = source

    def observe(self, bearings: dict[str, float], stamp: float,
                head_speeds: Optional[dict[str, float]] = None) -> None:
        for name, bearing in bearings.items():
            axis = self.axes.get(name)
            if axis is not None and math.isfinite(bearing):
                speed = (head_speeds or {}).get(name, 0.0)
                axis.observe(bearing, stamp, self.cfg, self.gaze, speed)

    def _duration(self, distance: float) -> float:
        cfg = self.cfg
        duration = min(cfg.move_base_s + cfg.move_scale_s_per_deg * distance, cfg.move_max_s)
        # Minimum-jerk peak speed is 1.875·d/T; stretch long moves to stay under the cap.
        return max(duration, 1.875 * distance / max(cfg.max_speed_deg_s, 1.0))

    def step(self, now: float, dt: float, limits: dict[str, tuple[float, float]]) -> dict[str, float]:
        bounds = {name: limits.get(name, (-math.inf, math.inf)) for name in self.axes}
        proposals: dict[str, tuple[float, float]] = {}
        for name, axis in self.axes.items():
            axis.settle_s = max(0.0, axis.settle_s - dt)
            if (self.source is None or axis.last_stamp is None
                    or now - axis.last_stamp > self.gaze.lost_s):
                continue
            estimate = axis.estimate(now, self.cfg, self.gaze)
            if estimate is None:
                continue
            lo, hi = bounds[name]
            desired, velocity = estimate
            desired = min(max(desired, lo), hi)
            if desired in (lo, hi):
                velocity = 0.0
            axis.desired = desired
            proposals[name] = (desired, velocity)
        if proposals:
            self._coordinate(proposals, bounds, dt)
        return {name: axis.advance(dt, *bounds[name], self.cfg)
                for name, axis in self.axes.items()}

    def _coordinate(self, proposals: dict[str, tuple[float, float]],
                    bounds: dict[str, tuple[float, float]], dt: float) -> None:
        """Decide jointly whether to move, then plan every axis with one duration."""
        cfg, gaze = self.cfg, self.gaze
        axes = {name: self.axes[name] for name in proposals}
        errors = {name: proposals[name][0] - axis.pos for name, axis in axes.items()}
        pursuing = any(axis.pursuing for axis in axes.values())
        at_limit = any(proposals[n][0] in bounds[n] for n in axes)
        distance = math.hypot(*errors.values())
        # Large re-fixations deliberately land short, like human saccades; a
        # small corrective move follows if still needed. While pursuing, large
        # errors get a velocity-matched catch-up instead.
        saccade = distance > gaze.saccade_min_deg and not pursuing and not at_limit
        finals, velocities = {}, {}
        for name, axis in axes.items():
            desired, velocity = proposals[name]
            if saccade:
                final, velocity = axis.pos + gaze.saccade_gain * errors[name], 0.0
            else:
                final = desired + velocity * self._duration(abs(errors[name]))
            lo, hi = bounds[name]
            finals[name] = min(max(final, lo), hi)
            velocities[name] = 0.0 if finals[name] in (lo, hi) else velocity

        if all(axis.planner.done for axis in axes.values()):
            fixated = all(axis.fixated for axis in axes.values())
            zone = 1 if fixated else 0
            outside = math.sqrt(sum((errors[n] / _zones(n, cfg, gaze)[zone]) ** 2
                                    for n in axes)) > 1.0
            replan = outside or pursuing
            if replan:
                self.dwell_s = 0.0
            else:
                # Lock fixation only after the estimate has stayed centred.
                self.dwell_s += dt
                if self.dwell_s >= gaze.fixation_dwell_s:
                    for axis in axes.values():
                        axis.fixated = True
        else:
            drift = max(abs(proposals[n][0] - axis.goal) if axis.goal is not None
                        else abs(finals[n] - axis.planner.target)
                        for n, axis in axes.items())
            replan = drift > cfg.replan_threshold_deg
        if not replan:
            return
        move_s = self._duration(math.hypot(*(finals[n] - a.pos for n, a in axes.items())))
        for name, axis in axes.items():
            axis.planner.plan(axis.pos, axis.vel, finals[name], move_s, velocities[name],
                              a0=axis.acc)
            if saccade:
                axis.settle_s = move_s + gaze.pursuit_settle_s
            axis.fixated = False
            axis.goal = proposals[name][0] if saccade else None
        self.dwell_s = 0.0

    def release(self) -> None:
        """Lose the target and coast to a natural stop."""
        if self.source is None:
            return
        self.source = None
        for axis in self.axes.values():
            axis.stop()

    def halt(self) -> None:
        """Drop all state immediately (motion unavailable)."""
        self.source = None
        self.axes = {}

    def debug_state(self) -> dict:
        pan = self.axes.get("pan")
        return {
            "mode": "pursuit" if pan and abs(pan.vel) > self.gaze.pursuit_min_deg_s
            else "tracking" if self.source else "coasting" if self.moving else "idle",
            "source": self.source,
            "servo_angle": pan.pos if pan else None,
            "target": pan.desired if pan and pan.desired is not None else (pan.pos if pan else None),
            "face_vel": pan.kalman.velocity or 0.0 if pan else 0.0,
            "axes": {n: {"pos": a.pos, "vel": a.vel, "desired": a.desired}
                     for n, a in self.axes.items()},
        }
