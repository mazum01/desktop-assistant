"""Tests for FaceKalman — 1-D Kalman filter for face centroid tracking."""
import pytest
from src.motion.face_kalman import FaceKalman


# ── Initialisation ────────────────────────────────────────────────────────────

def test_first_update_returns_measurement():
    """First measurement should be returned as-is (seeding the filter)."""
    k = FaceKalman()
    pos, vel = k.update(640.0, 0.05)
    assert pos == pytest.approx(640.0, abs=1.0)
    assert vel == pytest.approx(0.0, abs=1.0)


def test_predict_returns_none_before_init():
    k = FaceKalman()
    assert k.predict(0.1) is None


def test_predict_returns_float_after_init():
    k = FaceKalman()
    k.update(640.0, 0.05)
    p = k.predict(0.1)
    assert isinstance(p, float)


# ── Smoothing ────────────────────────────────────────────────────────────────

def test_kalman_smooths_noisy_stationary_signal():
    """For a stationary face with noise, Kalman output should converge
    to a value close to the true position."""
    import random
    rng = random.Random(42)
    k = FaceKalman(r=400.0)
    true_pos = 640.0
    dt = 0.05
    estimates = []
    for _ in range(100):
        noisy = true_pos + rng.gauss(0, 15.0)
        pos, _ = k.update(noisy, dt)
        estimates.append(pos)
    # Last 20 estimates should be close to true position
    avg = sum(estimates[-20:]) / 20
    assert abs(avg - true_pos) < 10.0, f"Filter avg {avg:.1f} far from true {true_pos}"


def test_kalman_tracks_linearly_moving_face():
    """For a face moving at constant velocity, the filter should track it."""
    k = FaceKalman(r=100.0, q_vel=200.0)
    pos = 300.0
    vel = 50.0  # px/s
    dt = 0.05
    for _ in range(40):
        pos += vel * dt
        k.update(pos, dt)
    # After convergence, the velocity estimate should be near the true velocity
    _, est_vel = k.update(pos + vel * dt, dt)
    assert abs(est_vel - vel) < 20.0, f"Estimated vel {est_vel:.1f} far from {vel}"


# ── Reset ─────────────────────────────────────────────────────────────────────

def test_reset_reinitialises_filter():
    """After reset, the next update should seed from scratch (velocity = 0)."""
    k = FaceKalman()
    k.update(640.0, 0.05)
    k.update(660.0, 0.05)
    k.reset()
    assert not k.initialised
    pos, vel = k.update(500.0, 0.05)
    assert pos == pytest.approx(500.0, abs=1.0)
    assert vel == pytest.approx(0.0, abs=1.0)


# ── Predict ───────────────────────────────────────────────────────────────────

def test_predict_extrapolates_velocity():
    """predict() should return position + velocity * t_ahead."""
    k = FaceKalman(r=1.0, q_vel=1000.0)  # trust velocity strongly
    # Feed a fast-moving sequence to build up velocity estimate
    for i in range(20):
        k.update(300.0 + i * 5.0, 0.05)
    # The predicted position should be ahead of the current smoothed position
    cur_pos, cur_vel = k.update(300.0 + 20 * 5.0, 0.05)
    pred = k.predict(0.1)
    assert pred > cur_pos, "predict() should be ahead of current for positive velocity"


# ── Variable dt ───────────────────────────────────────────────────────────────

def test_variable_dt_does_not_crash():
    """Variable inter-frame times should be handled cleanly."""
    import random
    rng = random.Random(7)
    k = FaceKalman()
    for _ in range(50):
        dt = rng.uniform(0.01, 0.15)
        pos, vel = k.update(640.0 + rng.gauss(0, 10), dt)
        assert isinstance(pos, float)
        assert isinstance(vel, float)
