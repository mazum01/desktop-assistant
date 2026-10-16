"""Visual controller, target association, and plant identification regressions."""

import json

import numpy as np
import pytest

from src.motion.visual_servo import (
    FaceLock, PlantModel, VisualController, VisualServoConfig, identify_plant,
)


def face(x=320, y=240, identity="one", confidence=0.9):
    return {"centroid": [x, y], "bbox": [x - 30, y - 30, x + 30, y + 30],
            "confidence": confidence, "face_id": identity}


def test_lock_requires_distinct_consistent_frames():
    lock = FaceLock(VisualServoConfig())
    assert lock.select([face()], 640, 480, 1, 1) is None
    assert lock.select([face()], 640, 480, 1, 1) is None
    assert lock.select([face()], 640, 480, 2, 2) is None
    assert lock.select([face()], 640, 480, 3, 3) is not None
    assert lock.select([face(600, identity="other")], 640, 480, 3.1, 3.1) is None
    assert lock.identity == "one"


def test_lock_does_not_switch_to_higher_confidence_face():
    lock = FaceLock(VisualServoConfig())
    for stamp in (1, 1.1, 1.2):
        lock.select([face()], 640, 480, stamp, stamp)
    selected = lock.select([face(330), face(600, identity="two", confidence=1)],
                           640, 480, 1.3, 1.3)
    assert selected[0]["face_id"] == "one"


def test_suspect_multi_face_and_stale_results_hold():
    lock = FaceLock(VisualServoConfig())
    for stamp in range(1, 6):
        assert lock.select([face(identity=None, confidence=1),
                            face(500, identity=None, confidence=1)],
                           640, 480, stamp, stamp) is None
    assert lock.select([face()], 640, 480, 6, 8) is None
    assert lock.select([face()], 640, 480, float("nan"), 8) is None


def model(tilt=True):
    return PlantModel([[0.01, 0.001], [0.0005, -0.015]] if tilt else [[0.01], [0]],
                      0.6, 0.001, {})


def test_one_correction_per_fresh_post_settling_frame():
    controller = VisualController(VisualServoConfig())
    result = controller.correction([0.2, -0.1], [90, 90], [[70, 110]] * 2, 1, 1, model())
    assert result is not None
    assert controller.correction([0.2, -0.1], [90, 90], [[70, 110]] * 2, 1, 1.5, model()) is None
    assert controller.correction([0.2, -0.1], [90, 90], [[70, 110]] * 2, 1.5, 1.5, model()) is None
    assert controller.correction([0.2, -0.1], [90, 90], [[70, 110]] * 2, 3, 5, model()) is None


def test_future_timestamp_does_not_poison_subsequent_valid_feedback():
    controller = VisualController(VisualServoConfig())
    assert controller.correction([0.2, 0], [90], [[70, 110]], 100, 1, model(False)) is None
    assert controller.correction([0.2, 0], [90], [[70, 110]], 2, 2, model(False)) is not None
    lock = FaceLock(VisualServoConfig())
    assert lock.select([face()], 640, 480, 100, 1) is None
    for stamp in (2, 2.1, 2.2):
        selected = lock.select([face()], 640, 480, stamp, stamp)
    assert selected is not None


@pytest.mark.parametrize("tilt", [False, True])
@pytest.mark.parametrize("delay", [0.1, 0.8, 1.8])
def test_delayed_noisy_plant_converges_without_limit_violation(tilt, delay):
    cfg = VisualServoConfig()
    controller = VisualController(cfg)
    plant = model(tilt)
    matrix = plant.matrix()
    axes = matrix.shape[1]
    position = np.full(axes, 90.0)
    error = np.array([0.12, -0.12 if tilt else 0.2])
    rng = np.random.default_rng(10)
    for step in range(80):
        now = step * 3.0 + 3
        result = controller.correction(
            error + rng.normal(0, 0.001, 2), position,
            [[70, 110]] * axes, now - delay, now, plant,
        )
        if result is not None:
            target, duration = result
            assert np.all((70 <= target) & (target <= 110))
            distance = np.max(np.abs(target - position))
            assert distance <= cfg.max_step_deg + 1e-9
            assert 1.875 * distance / duration <= cfg.max_speed_deg_s + 1e-9
            assert 5.78 * distance / duration ** 2 <= cfg.max_accel_deg_s2 + 1e-9
            error += matrix @ (target - position)
            position = target
    if delay <= cfg.max_age_s:
        assert np.max(np.abs(error[:axes])) <= 0.025
    else:
        np.testing.assert_equal(position, np.full(axes, 90))


def test_hysteresis_and_no_windup_at_limit():
    controller = VisualController(VisualServoConfig())
    plant = model(False)
    assert controller.correction([0.01, 0], [90], [[70, 110]], 1, 1, plant) is None
    assert controller.correction([0.025, 0], [90], [[70, 110]], 2, 2, plant) is None
    assert controller.correction([0.04, 0], [90], [[70, 110]], 3, 3, plant) is not None
    for stamp in (5, 7, 9):
        assert controller.correction([0.4, 0], [70], [[70, 110]], stamp, stamp, plant) is None
        assert controller.state == "at_limit"
    result = controller.correction([-0.1, 0], [70], [[70, 110]], 11, 11, plant)
    assert result[0][0] > 70


def probes(axes):
    commands = [np.zeros(axes)]
    for axis in range(axes):
        for sign in (1, -1, 1, -1):
            point = np.zeros(axes)
            point[axis] = sign * 2
            commands.extend([point, np.zeros(axes)])
    return np.asarray(commands)


@pytest.mark.parametrize("axes", [1, 2])
def test_identification_validation_and_atomic_persistence(axes, tmp_path):
    commands = probes(axes)
    matrix = model(axes == 2).matrix()
    points = commands @ matrix.T + [0.1, -0.05]
    fitted = identify_plant(commands, points, [1] * len(points), 0.001, {"axes": axes})
    np.testing.assert_allclose(fitted.matrix(), matrix, atol=1e-10)
    path = tmp_path / "model.json"
    fitted.save(path)
    loaded = PlantModel.load(path, {"axes": axes})
    np.testing.assert_allclose(loaded.matrix(), matrix)
    with pytest.raises(ValueError, match="does not match"):
        PlantModel.load(path, {"axes": 3})
    assert json.loads(path.read_text())["signature"] == {"axes": axes}


def test_inconsistent_moving_target_calibration_rejected():
    commands = probes(2)
    points = commands @ model().matrix().T
    points[-1] += 0.1
    with pytest.raises(ValueError, match="Held-out"):
        identify_plant(commands, points, [1], 0.001, {})
    with pytest.raises(ValueError):
        identify_plant(commands, np.zeros_like(points), [1], 0, {})


@pytest.mark.parametrize("kwargs", [
    {"max_age_s": float("nan")}, {"gain": 2}, {"max_step_deg": 10},
    {"stop_error": 0.1}, {"acquire_frames": 1}, {"max_speed_deg_s": 100},
])
def test_bad_configuration_rejected(kwargs):
    with pytest.raises(ValueError):
        VisualServoConfig(**kwargs)
