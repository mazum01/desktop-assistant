"""Tests for the person-like gaze tracker."""

from __future__ import annotations

import math
import random

import pytest

from src.motion.gaze_tracker import AngleHistory, GazeConfig, GazeTracker, image_offsets
from src.motion.head_tracker import HeadTrackerConfig, MinJerkPlanner

DT = 0.05


def make_cfg(**kwargs):
    base = dict(frame_width=640, frame_height=480, fov_degrees=100.0,
                dead_zone_frac=0.02, max_speed_deg_s=120.0, lookahead_s=0.02,
                move_base_s=0.18, move_scale_s_per_deg=0.012, move_max_s=0.6,
                replan_threshold_deg=2.0)
    base.update(kwargs)
    return HeadTrackerConfig(**base)


def simulate(target_fn, seconds, start=90.0, frame_hz=10.0, latency=0.1,
             noise_deg=0.0, cfg=None, stale_repeats=False, seed=0):
    """Closed loop: camera sees target relative to the head at capture time."""
    rng = random.Random(seed)
    cfg = cfg or make_cfg()
    tracker = GazeTracker(cfg)
    tracker.engage("face", {"pan": start})
    history = []
    pos = start
    out = []
    next_frame = 0.0
    pending = []
    last = None
    t = 0.0
    while t < seconds:
        history.append((t, pos))
        if t >= next_frame:
            pending.append((t + latency, t, target_fn(t) - pos + rng.gauss(0, noise_deg)))
            next_frame += 1 / frame_hz
        ready = [p for p in pending if p[0] <= t]
        pending = [p for p in pending if p[0] > t]
        for _, stamp, offset in ready:
            head_at = min(history, key=lambda h: abs(h[0] - stamp))[1]
            last = (head_at + offset, stamp)
            tracker.observe({"pan": head_at + offset}, stamp)
        if stale_repeats and last is not None:
            tracker.observe({"pan": last[0]}, last[1])
        pos = tracker.step(t, DT, {"pan": (40.0, 140.0)})["pan"]
        out.append((t, pos, target_fn(t)))
        t += DT
    return out


def test_planner_reaches_terminal_velocity():
    planner = MinJerkPlanner()
    planner.plan(0.0, 0.0, 10.0, 0.5, 20.0)
    for _ in range(10):
        pos, vel = planner.step(0.05)
    assert pos == pytest.approx(10.0)
    assert vel == pytest.approx(20.0)


@pytest.mark.parametrize("invert", [False, True])
def test_image_offsets_follow_pan_direction_and_logical_tilt(invert):
    cfg = make_cfg(invert_pan=invert)
    pan, tilt = image_offsets((0.25, -0.25), cfg)
    assert (pan < 0) is invert and abs(pan) > 10
    assert tilt > 0
    assert image_offsets((0.0, 0.0), cfg) == (0.0, 0.0)
    assert abs(image_offsets((0.5, 0.0), cfg)[0]) == pytest.approx(50.0)


def test_angle_history_interpolates_and_clamps():
    history = AngleHistory()
    assert history.at(1.0) is None
    history.add(1.0, 80.0, 90.0)
    history.add(2.0, 100.0, 70.0)
    assert history.at(1.5) == pytest.approx((90.0, 80.0))
    assert history.at(0.0) == (80.0, 90.0)
    assert history.at(9.0) == (100.0, 70.0)


@pytest.mark.parametrize("stale_repeats", [False, True])
def test_converges_without_overshoot_despite_latency_and_stale_frames(stale_repeats):
    out = simulate(lambda _t: 115.0, 3.0, stale_repeats=stale_repeats)
    positions = [p for _, p, _ in out]
    assert max(positions) <= 115.0 + 0.5
    # Residual error inside the settle zone is deliberately left alone.
    assert abs(positions[-1] - 115.0) <= GazeConfig().settle_pan_deg


def test_motion_is_smooth_and_speed_limited():
    out = simulate(lambda _t: 125.0, 3.0)
    vel = [(b[1] - a[1]) / DT for a, b in zip(out, out[1:])]
    acc = [(b - a) / DT for a, b in zip(vel, vel[1:])]
    assert max(abs(v) for v in vel) <= 120.0 + 1e-6
    assert max(abs(a) for a in acc) < 2500.0
    # A single re-fixation, not a staircase of stop-and-go corrections.
    moving = [abs(v) > 1.0 for v in vel]
    starts = sum(1 for a, b in zip(moving, moving[1:]) if b and not a)
    assert starts <= 2


def test_smooth_pursuit_matches_moving_target_without_stopping():
    out = simulate(lambda t: 70.0 + 15.0 * t, 3.0, noise_deg=0.3)
    steady = [o for o in out if 1.5 <= o[0] <= 3.0]
    vel = [(b[1] - a[1]) / DT for a, b in zip(steady, steady[1:])]
    assert min(vel) > 3.0
    assert sum(vel) / len(vel) == pytest.approx(15.0, abs=4.0)
    assert max(abs(target - pos) for _, pos, target in steady) < 6.0


def test_fixation_holds_still_on_jittery_stationary_target():
    out = simulate(lambda _t: 100.0, 6.0, noise_deg=0.6)
    late = [p for t, p, _ in out if t > 3.0]
    assert max(late) - min(late) < 1.5


def test_release_coasts_to_rest_then_stops_moving():
    cfg = make_cfg()
    tracker = GazeTracker(cfg)
    tracker.engage("face", {"pan": 90.0})
    for i in range(6):
        tracker.observe({"pan": 90.0 + 4.0 * i}, i * 0.1)
        tracker.step(i * 0.1, 0.1, {"pan": (40.0, 140.0)})
    tracker.release()
    assert not tracker.active
    positions = [tracker.step(1.0 + i * DT, DT, {"pan": (40.0, 140.0)})["pan"]
                 for i in range(40)]
    assert not tracker.moving
    assert positions[-1] == positions[-2]


def test_lost_target_holds_after_prediction_horizon():
    tracker = GazeTracker(make_cfg())
    tracker.engage("face", {"pan": 90.0})
    tracker.observe({"pan": 100.0}, 0.0)
    for i in range(80):
        pos = tracker.step(i * DT, DT, {"pan": (40.0, 140.0)})["pan"]
    assert pos == pytest.approx(100.0, abs=2.5)


def test_new_source_resets_filter_but_keeps_momentum():
    tracker = GazeTracker(make_cfg())
    tracker.engage("person", {"pan": 90.0})
    tracker.observe({"pan": 120.0}, 0.0)
    for i in range(4):
        tracker.step(i * DT, DT, {"pan": (40.0, 140.0)})
    velocity = tracker.axes["pan"].vel
    tracker.engage("face", {"pan": 0.0})
    assert tracker.axes["pan"].last_stamp is None
    assert tracker.axes["pan"].vel == velocity


def test_targets_respect_limits():
    tracker = GazeTracker(make_cfg())
    tracker.engage("face", {"pan": 100.0, "tilt": 90.0})
    tracker.observe({"pan": 300.0, "tilt": -50.0}, 0.0)
    for i in range(40):
        out = tracker.step(i * DT, DT, {"pan": (70.0, 110.0), "tilt": (70.0, 110.0)})
    assert out["pan"] == pytest.approx(110.0)
    assert out["tilt"] == pytest.approx(70.0)


@pytest.mark.parametrize("bad", [
    {"servo_lag_s": -1}, {"max_predict_s": 0}, {"pursuit_min_deg_s": 70},
    {"max_pursuit_deg_s": 500}, {"reacquire_jump_deg": 0}, {"lost_s": math.nan},
    {"lost_s": True}, {"saccade_min_deg": 1}, {"saccade_gain": 1.2},
    {"settle_pan_deg": 7}, {"release_tilt_deg": 1}, {"pursuit_settle_s": 3},
    {"pursuit_confidence": 6}, {"fixation_dwell_s": -0.1},
])
def test_invalid_gaze_config_rejected(bad):
    with pytest.raises(ValueError):
        GazeConfig(**bad)


def test_vertical_fov_override_drives_tilt_offsets():
    derived = make_cfg(fov_degrees=72.0)
    measured = make_cfg(fov_degrees=72.0, vertical_fov_degrees=49.0)
    assert derived.vertical_fov() == pytest.approx(
        math.degrees(2 * math.atan(math.tan(math.radians(36.0)) * 480 / 640)))
    assert measured.vertical_fov() == 49.0
    assert image_offsets((0.0, -0.5), measured)[1] == pytest.approx(24.5)
    assert image_offsets((0.5, 0.0), measured)[0] == pytest.approx(36.0)


def test_large_refixation_lands_short_then_corrects():
    tracker = GazeTracker(make_cfg())
    tracker.engage("face", {"pan": 90.0})
    tracker.observe({"pan": 120.0}, 0.0)
    tracker.step(0.0, DT, {"pan": (40.0, 140.0)})
    axis = tracker.axes["pan"]
    assert axis.planner.target == pytest.approx(90.0 + 0.9 * 30.0, abs=0.5)
    out = simulate(lambda _t: 120.0, 3.0)
    assert max(p for _, p, _ in out) <= 120.5


def test_settled_fixation_ignores_small_drift_but_follows_larger_moves():
    tracker = GazeTracker(make_cfg(fov_degrees=72.0))
    tracker.engage("face", {"pan": 90.0})
    limits = {"pan": (40.0, 140.0)}
    t = 0.0
    for _ in range(40):
        tracker.observe({"pan": 95.0}, t)
        tracker.step(t, DT, limits)
        t += DT
    settled = tracker.axes["pan"].pos
    assert tracker.axes["pan"].fixated
    for _ in range(40):
        tracker.observe({"pan": settled + 4.5}, t)
        pos = tracker.step(t, DT, limits)["pan"]
        t += DT
    assert pos == pytest.approx(settled)
    for _ in range(40):
        tracker.observe({"pan": settled + 7.0}, t)
        pos = tracker.step(t, DT, limits)["pan"]
        t += DT
    assert pos == pytest.approx(settled + 7.0, abs=1.5)


def test_pursuit_waits_for_saccade_to_settle():
    tracker = GazeTracker(make_cfg())
    tracker.engage("face", {"pan": 90.0})
    tracker.observe({"pan": 120.0}, 0.0)
    tracker.step(0.0, DT, {"pan": (40.0, 140.0)})
    axis = tracker.axes["pan"]
    assert axis.settle_s > GazeConfig().pursuit_settle_s
    axis.kalman._x[1] = 20.0
    assert tracker.axes["pan"].estimate(DT, make_cfg(), GazeConfig()) is not None
    assert not axis.pursuing


def test_uncertain_velocity_does_not_start_pursuit():
    tracker = GazeTracker(make_cfg())
    tracker.engage("face", {"pan": 90.0})
    axis = tracker.axes["pan"]
    tracker.observe({"pan": 91.0}, 0.0)
    axis.kalman._x[1] = 6.0
    axis.kalman._P[1, 1] = 16.0
    axis.estimate(0.0, make_cfg(), GazeConfig())
    assert not axis.pursuing
    axis.kalman._P[1, 1] = 1.0
    axis.estimate(0.0, make_cfg(), GazeConfig())
    assert axis.pursuing


def test_planner_preserves_acceleration_across_replans():
    planner = MinJerkPlanner()
    planner.plan(0.0, 0.0, 10.0, 0.5)
    for _ in range(3):
        pos, vel = planner.step(DT)
    acc = planner.acc
    assert acc > 10.0
    planner.plan(pos, vel, 12.0, 0.5, a0=acc)
    assert planner.acc == pytest.approx(acc)
    before = planner.acc
    planner.step(1e-4)
    assert planner.acc == pytest.approx(before, rel=0.01)
    planner.step(1.0)
    assert planner.acc == pytest.approx(0.0, abs=1e-6)


def diagonal_path(pan_target, tilt_target):
    tracker = GazeTracker(make_cfg(fov_degrees=72.0, vertical_fov_degrees=49.0))
    tracker.engage("face", {"pan": 90.0, "tilt": 90.0})
    limits = {"pan": (40.0, 140.0), "tilt": (60.0, 120.0)}
    path = []
    for i in range(60):
        t = i * DT
        tracker.observe({"pan": pan_target, "tilt": tilt_target}, t)
        out = tracker.step(t, DT, limits)
        path.append((out["pan"], out["tilt"]))
    return tracker, path


def test_diagonal_shift_moves_both_axes_in_a_straight_line():
    _, path = diagonal_path(110.0, 96.0)
    moving = [(p - 90.0, q - 90.0) for p, q in path if abs(p - 90.0) > 0.5]
    assert moving
    # Shared timing keeps tilt/pan progress proportional along the move.
    for dp, dq in moving:
        assert dq / dp == pytest.approx(6.0 / 20.0, abs=0.03)


def test_elliptical_zone_combines_axes():
    # Each axis alone is inside the settle zone, but the combination is not.
    tracker, path = diagonal_path(92.5, 91.8)
    assert path[-1][0] == pytest.approx(92.5, abs=0.3)
    assert path[-1][1] == pytest.approx(91.8, abs=0.3)
    tracker, path = diagonal_path(91.5, 91.0)
    assert path[-1] == (90.0, 90.0)
