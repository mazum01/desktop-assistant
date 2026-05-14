"""Tests for HeadTracker — Kalman + min-jerk dynamics, dead zone, idle state."""
import math
import pytest

from src.motion.head_tracker import HeadTracker, HeadTrackerConfig, _IdleState


FRAME_WIDTH = 1280
FOV = 100.0


@pytest.fixture
def tracker():
    cfg = HeadTrackerConfig(
        frame_width=FRAME_WIDTH,
        fov_degrees=FOV,
        dead_zone_frac=0.04,
        tracking_gain=0.92,
        max_speed_deg_s=250.0,
        move_base_s=0.10,
        move_scale_s_per_deg=0.004,
        move_max_s=0.5,
        replan_threshold_deg=1.5,
    )
    return HeadTracker(initial_servo_angle=180.0, config=cfg)


# ── Tracking convergence ─────────────────────────────────────────────────────

def test_tracker_converges_toward_target(tracker):
    """Angle should move toward a face off-centre over several steps."""
    cx = 900  # right of centre → servo should pan
    angles = []
    dt = 0.05  # 20 Hz
    for _ in range(60):
        a = tracker.update(cx, dt)
        angles.append(a)
    assert angles[-1] != angles[0], "tracker never moved"
    assert angles[-1] != pytest.approx(180.0, abs=0.1)


def test_tracker_stays_near_centre_when_face_centred(tracker):
    """When face is exactly at frame centre, tracker should barely move."""
    cx = FRAME_WIDTH // 2
    start = tracker.update(cx, 0.05)
    for _ in range(20):
        tracker.update(cx, 0.05)
    end = tracker.update(cx, 0.05)
    assert abs(end - start) < 5.0


# ── Dead zone ────────────────────────────────────────────────────────────────

def test_dead_zone_prevents_movement_near_centre(tracker):
    """Tiny offsets within dead zone should produce no movement."""
    cx = FRAME_WIDTH // 2 + 10  # tiny offset — inside dead zone
    a0 = tracker.update(cx, 0.05)
    a1 = tracker.update(cx, 0.05)
    assert abs(a1 - a0) < 1.0


# ── Speed cap ────────────────────────────────────────────────────────────────

def test_max_speed_capped(tracker):
    """Position change per tick must not imply speed > max_speed_deg_s."""
    cx = 0  # extreme left — large error
    dt = 0.05
    prev = 180.0
    for _ in range(60):
        a = tracker.update(cx, dt)
        speed = abs(a - prev) / dt
        assert speed <= 250.0 + 2.0, f"speed {speed:.1f} exceeded cap"
        prev = a


# ── Min-jerk smoothness ───────────────────────────────────────────────────────

def test_min_jerk_produces_smooth_velocity(tracker):
    """Velocity profile should be unimodal (bell curve) for a single saccade."""
    cx = 960  # 25% right of centre
    dt = 0.05
    angles = [tracker.update(cx, dt) for _ in range(30)]
    speeds = [abs(angles[i+1] - angles[i]) / dt for i in range(len(angles)-1)]
    # For a smooth bell-curve the max speed should occur somewhere in the middle,
    # not at the very first or very last step (after convergence)
    peak_idx = speeds.index(max(speeds))
    assert 0 < peak_idx < len(speeds) - 1, (
        f"Peak speed at index {peak_idx} — not a bell curve"
    )


# ── Face-lost handoff ─────────────────────────────────────────────────────────

def test_smooth_deceleration_on_face_lost(tracker):
    """When face is lost mid-saccade the head should decelerate, not snap."""
    cx = 100  # far left — head is moving
    dt = 0.05
    for _ in range(5):
        tracker.update(cx, dt)
    # Now lose the face
    angles_after = [tracker.update(None, dt) for _ in range(20)]
    # Position should not jump abruptly
    for i in range(1, len(angles_after)):
        delta = abs(angles_after[i] - angles_after[i-1])
        assert delta < 15.0, f"Abrupt jump of {delta:.1f}° after face lost"


# ── Idle state ───────────────────────────────────────────────────────────────

def test_idle_mode_when_no_face(tracker):
    """Calling update(None, dt) should trigger idle mode without crash."""
    for _ in range(100):
        a = tracker.update(None, 0.05)
    assert isinstance(a, float)
    assert 1.0 <= a <= 360.0


def test_idle_stays_within_mechanical_range(tracker):
    """Idle gaze targets must stay within the servo's logical range [1–360]."""
    for _ in range(200):
        a = tracker.update(None, 0.05)
        assert 1.0 <= a <= 360.0, f"Idle angle {a} out of range"


# ── update_config / get_config / debug state ────────────────────────────────

def test_update_config_accepts_valid_field(tracker):
    assert tracker.update_config("tracking_gain", 0.45) is True
    assert tracker.get_config()["tracking_gain"] == pytest.approx(0.45)


def test_update_config_rejects_unknown_field(tracker):
    assert tracker.update_config("not_a_real_field", 1.0) is False


def test_update_config_rejects_out_of_range(tracker):
    assert tracker.update_config("tracking_gain", 99.0) is False
    assert tracker.update_config("tracking_gain", -1.0) is False


def test_update_config_kalman_propagates(tracker):
    assert tracker.update_config("kalman_r", 750.0) is True
    # Force a tick so Kalman is in use, then check property
    tracker.update(640, 0.05)
    assert float(tracker._kalman.r) == pytest.approx(750.0)


def test_get_config_returns_all_tunable_fields(tracker):
    cfg = tracker.get_config()
    expected = {"tracking_gain", "dead_zone_frac", "max_speed_deg_s",
                "kalman_r", "kalman_q_pos", "kalman_q_vel",
                "lookahead_s", "replan_threshold_deg",
                "move_base_s", "move_scale_s_per_deg", "move_max_s"}
    assert expected.issubset(set(cfg.keys()))


def test_debug_state_updates_on_tracking(tracker):
    tracker.update(900, 0.05)
    dbg = tracker.get_debug_state()
    assert dbg["mode"] == "tracking"
    assert dbg["face_raw"] == 900
    assert dbg["face_smoothed"] is not None
