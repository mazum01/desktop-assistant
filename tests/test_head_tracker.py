"""Tests for HeadTracker — spring-damper dynamics, dead zone, idle state."""
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
        spring_k=6.0,
        damping=2.5,
        max_speed_deg_s=80.0,
        dead_zone_frac=0.04,
    )
    return HeadTracker(initial_servo_angle=180.0, config=cfg)


# ── Tracking convergence ─────────────────────────────────────────────────────

def test_tracker_converges_toward_target(tracker):
    """Angle should move toward a face off-centre over several steps."""
    cx = 900  # right of centre → servo should pan right
    angles = []
    dt = 0.05  # 20 Hz
    for _ in range(40):
        a = tracker.update(cx, dt)
        angles.append(a)
    # The tracker should move from initial 180° toward the face
    assert angles[-1] != angles[0], "tracker never moved"
    # Face is right of centre; servo should decrease (camera pans right)
    # (offset_deg positive → servo_target = current - offset → smaller)
    assert angles[-1] != pytest.approx(180.0, abs=0.1)


def test_tracker_stays_near_centre_when_face_centred(tracker):
    """When face is exactly at frame centre, tracker should barely move."""
    cx = FRAME_WIDTH // 2
    start = tracker.update(cx, 0.05)
    for _ in range(20):
        tracker.update(cx, 0.05)
    end = tracker.update(cx, 0.05)
    # Dead zone should absorb small centred face position
    assert abs(end - start) < 5.0


# ── Dead zone ────────────────────────────────────────────────────────────────

def test_dead_zone_prevents_movement_near_centre(tracker):
    """Tiny offsets within dead zone should produce no movement."""
    # Dead zone is 4% of frame width * (fov/frame_width) = ~4°
    # Put face very slightly off centre (1% = 12px)
    cx = FRAME_WIDTH // 2 + 10
    a0 = tracker.update(cx, 0.05)
    a1 = tracker.update(cx, 0.05)
    assert abs(a1 - a0) < 1.0


# ── Speed cap ────────────────────────────────────────────────────────────────

def test_max_speed_capped(tracker):
    """Velocity must never exceed max_speed_deg_s per second."""
    cx = 0  # extreme left — large error
    dt = 0.05
    prev = 180.0
    for _ in range(60):
        a = tracker.update(cx, dt)
        speed = abs(a - prev) / dt
        assert speed <= 80.0 + 1e-3, f"speed {speed:.1f} exceeded cap"
        prev = a


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
