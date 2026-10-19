"""Tests for TrackingService person-seek behaviour."""

from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from src.services.tracking_service import TrackingService, _PERSON_SEEK_STALE_S
from src.motion.head_tracker import HeadTrackerConfig
from src.motion.visual_servo import PlantModel
from src.core.bus import MessageBus
import numpy as np


def make_tracking_svc(**kwargs) -> tuple[TrackingService, MagicMock]:
    bus = MagicMock()
    bus.subscribe.return_value = lambda: None
    bus.last.return_value = None
    kwargs.setdefault("config", HeadTrackerConfig(frame_width=640, frame_height=480))
    svc = TrackingService(bus=bus, **kwargs)
    return svc, bus


# ---------------------------------------------------------------------------
# Constructor defaults
# ---------------------------------------------------------------------------

def test_person_seek_enabled_default():
    svc, _ = make_tracking_svc()
    assert svc.person_seek_enabled is True


def test_person_seek_disabled_via_ctor():
    svc, _ = make_tracking_svc(person_seek_enabled=False)
    assert svc.person_seek_enabled is False


def make_legacy_svc(**kwargs):
    kwargs.setdefault("config", HeadTrackerConfig(
        frame_width=640, frame_height=480, servo_min=135, servo_max=215,
        servo_center=175, saccade_amplitude_deg=0,
    ))
    svc, bus = make_tracking_svc(controller="legacy", body="original", **kwargs)
    svc.on_start()
    bus.publish.reset_mock()
    return svc, bus


def test_legacy_accepts_one_face_without_visual_lock_or_capture_timestamp():
    svc, bus = make_legacy_svc(random_motion_enabled=False)
    svc._on_faces(None, {"faces": [
        {"centroid": [100, 200], "confidence": 0.6},
        {"centroid": [500, 200], "confidence": 0.9},
    ]})
    assert svc._current_face_cx == 500
    assert svc._observation is None
    with patch.object(svc._tracker, "update", return_value=180) as update:
        svc._legacy_tick(time.monotonic(), 0.05)
    update.assert_called_once_with(500, 0.05, None)
    assert bus.publish.call_args_list[0] == call("motion.pan_to", {
        "angle": 180, "move_time_ms": 100, "override_quiet": True,
    })


@pytest.mark.parametrize("x,direction", [(80, -1), (560, 1)])
def test_legacy_face_drives_original_pan_in_legacy_direction_within_limits(x, direction):
    svc, bus = make_legacy_svc(random_motion_enabled=False)
    position = 175
    angles = []
    for _ in range(30):
        svc._on_faces(None, {"faces": [{"centroid": [x, 200], "confidence": 0.9}]})
        svc._on_position(None, {"angle": position})
        svc._legacy_tick(time.monotonic(), 0.05)
        pan = [c.args[1] for c in bus.publish.call_args_list if c.args[0] == "motion.pan_to"][-1]
        position = pan["angle"]
        angles.append(position)
    assert (position - 175) * direction > 2
    assert all(135 <= a <= 215 for a in angles)
    assert all(c.args[0] != "motion.tilt_to" for c in bus.publish.call_args_list)


def test_legacy_loop_routes_to_legacy_not_visual_controller():
    svc, _ = make_legacy_svc()
    with patch.object(svc, "_legacy_tick") as legacy, patch.object(svc, "_visual_tick") as visual:
        run_one_tick(svc)
    legacy.assert_called_once()
    visual.assert_not_called()


@pytest.mark.parametrize("reason", ["missing", "stale", "disabled", "invalid"])
def test_legacy_holds_without_valid_active_face_or_person(reason):
    svc, bus = make_legacy_svc(random_motion_enabled=False)
    svc._on_faces(None, {"faces": [{"centroid": [500, 200]}]})
    if reason == "missing":
        svc._on_faces(None, {"faces": []})
    elif reason == "stale":
        svc._face_ts -= 3
    elif reason == "disabled":
        svc._on_set_face_tracking(None, {"enabled": False})
    else:
        svc._on_faces(None, {"faces": [{"centroid": [float("nan"), 200]}]})
    bus.publish.reset_mock()
    svc._legacy_tick(time.monotonic(), 0.05)
    bus.publish.assert_not_called()


def test_legacy_person_seek_is_fallback_and_expires():
    svc, bus = make_legacy_svc(random_motion_enabled=False)
    svc._on_objects(None, make_obj_payload([(100, 50, 300, 400, 0.9)]))
    with patch.object(svc._tracker, "update", return_value=170) as update:
        svc._legacy_tick(time.monotonic(), 0.05)
    update.assert_called_once_with(200, 0.05, None)
    svc._person_cx_ts -= 3
    bus.publish.reset_mock()
    svc._legacy_tick(time.monotonic(), 0.05)
    bus.publish.assert_not_called()


def test_legacy_face_takes_priority_over_person_seek():
    svc, _ = make_legacy_svc(random_motion_enabled=False)
    svc._on_objects(None, make_obj_payload([(100, 50, 300, 400, 0.9)]))
    svc._on_faces(None, {"faces": [{"centroid": [500, 200]}]})
    with patch.object(svc._tracker, "update", return_value=180) as update:
        svc._legacy_tick(time.monotonic(), 0.05)
    update.assert_called_once_with(500, 0.05, None)


def test_legacy_speaking_motion_stays_inside_original_limits():
    svc, bus = make_legacy_svc(speaking_motion_amplitude_deg=1000)
    svc._speaking_until = 100
    with patch.object(svc._tracker, "update", return_value=215):
        svc._legacy_tick(0.1, 0.05)
    assert bus.publish.call_args_list[0].args[1]["angle"] == 215


def test_legacy_random_motion_respects_quiet_hours_and_never_tilts():
    svc, bus = make_legacy_svc(random_motion_enabled=True)
    enable_tilt(svc)
    assert svc._tilt_tracker is None
    svc._legacy_tick(time.monotonic(), 0.05)
    pan = bus.publish.call_args_list[0]
    assert pan.args[0] == "motion.pan_to"
    assert pan.args[1]["override_quiet"] is False
    assert all(c.args[0] != "motion.tilt_to" for c in bus.publish.call_args_list)


def test_legacy_rejects_visual_calibration_without_moving():
    svc, bus = make_legacy_svc()
    svc._on_start_calibration(None, {})
    assert not svc._calibration_requested
    assert bus.publish.call_args.args[0] == "tracking.autotune_done"
    assert bus.publish.call_args.args[1]["ok"] is False
    assert svc.get_tunable_params()["controller"] == "legacy"
    assert svc.get_tunable_params()["body"] == "original"


# ---------------------------------------------------------------------------
# _on_set_person_seek
# ---------------------------------------------------------------------------

def test_set_person_seek_toggles_and_publishes():
    svc, bus = make_tracking_svc()
    svc._on_set_person_seek(None, {"enabled": False})
    assert svc.person_seek_enabled is False
    bus.publish.assert_called_with("tracking.person_seek_changed", {"enabled": False})


def test_set_person_seek_no_change_no_publish():
    svc, bus = make_tracking_svc()
    svc._on_set_person_seek(None, {"enabled": True})  # already True
    bus.publish.assert_not_called()


def test_set_person_seek_disable_clears_hint():
    svc, bus = make_tracking_svc()
    svc._person_cx = 200.0
    svc._person_cx_ts = time.monotonic()
    svc._on_set_person_seek(None, {"enabled": False})
    assert svc._person_cx is None
    assert svc._person_cx_ts == 0.0


# ---------------------------------------------------------------------------
# _on_objects — hint population
# ---------------------------------------------------------------------------

def make_obj_payload(persons: list[tuple[float, float, float, float, float]], frame_w: int = 640):
    """Build a perception.objects payload with person detections."""
    return {
        "frame_w": frame_w,
        "frame_h": 480,
        "objects": [
            {
                "label": "person",
                "class_id": 0,
                "confidence": conf,
                "bbox": [x1, y1, x2, y2],
            }
            for (x1, y1, x2, y2, conf) in persons
        ],
        "count": len(persons),
    }


def test_on_objects_sets_person_cx():
    svc, _ = make_tracking_svc()
    # person bbox x1=100, x2=300 → cx = 200
    payload = make_obj_payload([(100, 50, 300, 400, 0.9)])
    svc._on_objects(None, payload)
    assert svc._person_cx == pytest.approx(200.0)
    assert svc._person_cx_ts > 0.0


def test_on_objects_picks_highest_confidence():
    svc, _ = make_tracking_svc()
    payload = make_obj_payload([
        (0, 0, 100, 200, 0.5),   # cx=50
        (200, 0, 400, 200, 0.9), # cx=300  ← should win
    ])
    svc._on_objects(None, payload)
    assert svc._person_cx == pytest.approx(300.0)


def test_on_objects_clears_hint_when_no_persons():
    svc, _ = make_tracking_svc()
    svc._person_cx = 200.0
    svc._person_cx_ts = time.monotonic()
    payload = {
        "frame_w": 640, "frame_h": 480,
        "objects": [{"label": "cat", "confidence": 0.8, "bbox": [0, 0, 100, 100]}],
        "count": 1,
    }
    svc._on_objects(None, payload)
    assert svc._person_cx is None


def test_on_objects_ignored_when_person_seek_disabled():
    svc, _ = make_tracking_svc(person_seek_enabled=False)
    payload = make_obj_payload([(100, 0, 300, 400, 0.9)])
    svc._on_objects(None, payload)
    assert svc._person_cx is None


def test_on_objects_ignored_when_face_tracking_disabled():
    svc, _ = make_tracking_svc(face_tracking_enabled=False)
    payload = make_obj_payload([(100, 0, 300, 400, 0.9)])
    svc._on_objects(None, payload)
    assert svc._person_cx is None


# ---------------------------------------------------------------------------
# Staleness — loop uses fresh hint but ignores stale one
# ---------------------------------------------------------------------------

def test_person_cx_stale_when_old():
    svc, _ = make_tracking_svc()
    svc._person_cx = 200.0
    # Backdate the timestamp beyond the stale threshold
    svc._person_cx_ts = time.monotonic() - (_PERSON_SEEK_STALE_S + 0.1)

    # Simulate what _loop reads: stale → effective_cx should be None
    now = time.monotonic()
    face_cx = None
    person_cx = None
    if (
        face_cx is None
        and svc._person_seek_enabled
        and svc._person_cx is not None
        and (now - svc._person_cx_ts) < _PERSON_SEEK_STALE_S
    ):
        person_cx = svc._person_cx

    assert person_cx is None  # stale → ignored


def test_person_cx_fresh_when_recent():
    svc, _ = make_tracking_svc()
    svc._person_cx = 200.0
    svc._person_cx_ts = time.monotonic()  # just now

    now = time.monotonic()
    face_cx = None
    person_cx = None
    if (
        face_cx is None
        and svc._person_seek_enabled
        and svc._person_cx is not None
        and (now - svc._person_cx_ts) < _PERSON_SEEK_STALE_S
    ):
        person_cx = svc._person_cx

    assert person_cx == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# Face takes priority over person seek
# ---------------------------------------------------------------------------

def test_face_overrides_person_seek():
    """When a face_cx is available, person_cx must not be used."""
    svc, _ = make_tracking_svc()
    svc._person_cx = 200.0
    svc._person_cx_ts = time.monotonic()
    face_cx = 320.0  # face locked

    # Reproduce loop selection logic:
    now = time.monotonic()
    person_cx = None
    if (
        face_cx is None
        and svc._person_seek_enabled
        and svc._person_cx is not None
        and (now - svc._person_cx_ts) < _PERSON_SEEK_STALE_S
    ):
        person_cx = svc._person_cx

    effective_cx = face_cx if face_cx is not None else person_cx
    assert effective_cx == pytest.approx(320.0)  # face wins


def enable_tilt(svc, mn=70, mx=110):
    svc._on_motion_settings(None, {
        "tilt_available": True, "tilt_min_deg": mn, "tilt_max_deg": mx,
    })


def run_one_tick(svc):
    svc._stop_event.clear()
    with patch(
        "src.services.tracking_service.time.monotonic", return_value=time.monotonic(),
    ), patch.object(
        svc._stop_event, "wait",
        side_effect=lambda _: svc._stop_event.set(),
    ):
        svc._run_loop()


def feed_face(svc, x=400, y=100, **metadata):
    svc._motion_settings.update(servo_enabled=True)
    svc._motion_settings.update(hardware_ready=True)
    for _ in range(3):
        svc._on_faces(None, {
            "faces": [{"centroid": [x, y], "bbox": [x-30, y-30, x+30, y+30],
                       "confidence": 0.9}],
            "captured_mono": time.monotonic(), **metadata,
        })


def test_face_geometry_uses_detection_dimensions():
    svc, _ = make_tracking_svc()
    enable_tilt(svc)
    feed_face(svc, 540, 200, frame_w=1080, frame_h=1920)
    assert svc._tracker_cfg.frame_width == 1080
    assert svc._tracker_cfg.frame_height == 1920
    assert svc._tilt_tracker._cfg.frame_width == 1920
    assert svc._current_face_cy == 200
    svc.update_frame_width(1920, 1080)
    assert svc._tilt_tracker._cfg.frame_width == 1080


def test_person_seek_scales_to_tracking_frame():
    svc, _ = make_tracking_svc(config=HeadTrackerConfig(frame_width=1920))
    svc._on_objects(None, make_obj_payload([(100, 0, 300, 400, 0.9)], frame_w=640))
    assert svc._person_cx == 600


@pytest.mark.parametrize("offset", [-10, 10])
@pytest.mark.parametrize("invert", [False, True])
def test_tilt_centers_vertically_with_mount_direction(offset, invert):
    cfg = HeadTrackerConfig(
        frame_width=1920, frame_height=1080, tracking_gain=0.28,
        dead_zone_frac=0.02, invert_tilt=invert,
    )
    svc, _ = make_tracking_svc(config=cfg)
    enable_tilt(svc, 0, 180)
    tracker = svc._tilt_tracker
    assert tracker._cfg.servo_min == 70
    assert tracker._cfg.servo_max == 110
    position = 90.0
    direction = -1 if invert else 1
    for _ in range(300):
        error = offset - direction * (position - 90)
        cy = 540 + error / tracker._cfg.fov_degrees * 1080
        target = tracker.update(cy, 0.05, position)
        assert 70 <= target <= 110
        position += (target - position) * 0.5
    assert abs(error) <= tracker._cfg.fov_degrees * 0.021


@pytest.mark.parametrize("mn,mx", [(0, 180), (80, 100), (80, 115)])
@pytest.mark.parametrize("cy", [-10000, 10000])
def test_tilt_clamps_to_twenty_degrees_and_hardware_limits(mn, mx, cy):
    svc, _ = make_tracking_svc()
    enable_tilt(svc, mn, mx)
    center = (mn + mx) / 2
    for _ in range(200):
        target = svc._tilt_tracker.update(cy, 0.05)
        assert max(mn, center - 20) <= target <= min(mx, center + 20)


@pytest.mark.parametrize("has_tilt", [False, True])
def test_loop_only_publishes_tilt_when_available_and_face_visible(has_tilt):
    svc, bus = make_tracking_svc(random_motion_enabled=False)
    svc._tracker = MagicMock()
    svc._tracker.update.return_value = 90
    if has_tilt:
        enable_tilt(svc)
    svc._on_position(None, {"angle": 90, "tilt_angle": 90 if has_tilt else None})
    feed_face(svc)
    run_one_tick(svc)
    topics = [c.args[0] for c in bus.publish.call_args_list]
    assert "motion.pan_to" in topics
    assert ("motion.tilt_to" in topics) is has_tilt


@pytest.mark.parametrize("reason", ["disabled", "lost", "stale", "missing_feedback", "unavailable"])
def test_tilt_holds_without_current_face_or_hardware(reason):
    svc, bus = make_tracking_svc(random_motion_enabled=False)
    svc._tracker = MagicMock()
    svc._tracker.update.return_value = 90
    enable_tilt(svc)
    svc._on_position(None, {"angle": 90, "tilt_angle": 90})
    feed_face(svc)
    if reason == "disabled":
        svc._on_set_face_tracking(None, {"enabled": False})
    elif reason == "lost":
        svc._on_faces(None, {"faces": [], "captured_mono": time.monotonic()})
    elif reason == "stale":
        svc._face_ts -= 3
        point, stamp, arrived, count = svc._observation
        svc._observation = (point, stamp - 3, arrived, count)
    elif reason == "missing_feedback":
        svc._on_position(None, {"angle": 90})
    else:
        svc._on_motion_settings(None, {"tilt_available": False})
    bus.publish.reset_mock()
    run_one_tick(svc)
    assert "motion.tilt_to" not in [c.args[0] for c in bus.publish.call_args_list]


def test_tracking_lifecycle_has_one_owned_worker():
    svc, _ = make_tracking_svc(random_motion_enabled=False)
    with patch("threading.Thread", wraps=threading.Thread) as threads:
        for cycle in range(2):
            svc.start()
            worker = svc._thread
            svc.stop()
            assert not worker.is_alive()
            assert threads.call_count == cycle + 1


@pytest.mark.parametrize("tilt", [False, True])
def test_calibration_requires_announcements_and_saves_only_validated_model(tmp_path, tilt):
    bus = MessageBus()
    path = tmp_path / "plant.json"
    svc = TrackingService(bus=bus, visual_config={"calibration_path": str(path)})
    settings = {
        "servo_enabled": True, "hardware_ready": True, "tilt_available": tilt,
        "tilt_min_deg": 70, "tilt_max_deg": 110,
        "pan_channel": 1, "tilt_channel": 0,
    }
    svc._motion_settings = settings
    svc._current_servo_angle = 90
    svc._current_tilt_angle = 90 if tilt else None
    origin = np.full(2 if tilt else 1, 90.0)
    positions = origin.copy()
    matrix = np.array([[0.01, 0.001], [0.001, -0.015]]) if tilt else np.array([[0.01], [0]])
    limits = np.array([[70, 110]] * len(origin))
    now = 10.0
    observation = (np.array([0.005, 0.005]), now, now, 1)
    svc._begin_calibration(now, observation, positions, limits)
    assert not path.exists()
    assert bus.last("motion.calibration_lock") == {"enabled": True}

    def move(axis, payload):
        assert bus.last("av.spoke") is not None
        assert payload["source"] == "calibration"
        assert payload["profile"] == "smoothstep"
        positions[axis] = payload["angle"]
        assert 70 <= positions[axis] <= 110

    bus.subscribe("motion.pan_to", lambda _, p: move(0, p))
    if tilt:
        bus.subscribe("motion.tilt_to", lambda _, p: move(1, p))
    phases = set()
    for _ in range(1000):
        state = svc._calibration
        if state is None:
            break
        phases.add(state["phase"])
        if state["phase"] in ("speech", "validation_speech"):
            bus.publish("av.spoke", {"request_id": state["token"]})
        now += 0.25
        point = np.array([0.005, 0.005]) + matrix @ (positions - origin)
        observation = (point, now, now, 1)
        svc._calibration_tick(now, observation, positions.copy(), limits)
    assert "validate" in phases
    assert svc._calibration is None
    assert bus.last("tracking.autotune_done")["ok"] is True
    fitted = PlantModel.load(path, svc._signature())
    np.testing.assert_allclose(fitted.matrix(), matrix, atol=1e-10)
    assert bus.last("motion.calibration_lock") == {"enabled": False}


@pytest.mark.parametrize("failure", ["lost", "stale", "cancel", "limits", "timeout"])
def test_calibration_failure_preserves_previous_model_and_releases_motion(tmp_path, failure):
    svc, bus = make_tracking_svc(visual_config={"calibration_path": str(tmp_path / "model.json")})
    path = tmp_path / "model.json"
    path.write_text("previous model")
    svc._motion_settings = {"servo_enabled": True, "hardware_ready": True}
    positions = np.array([90.0])
    limits = np.array([[70.0, 110.0]])
    obs = (np.array([0.1, 0.1]), 10.0, 10.0, 1)
    svc._begin_calibration(10, obs, positions, limits)
    assert svc._calibration is not None
    now = 10.1
    if failure == "lost":
        obs = None
    elif failure == "stale":
        now = 12
    elif failure == "cancel":
        svc._on_cancel_calibration(None, {})
    elif failure == "limits":
        svc._motion_settings["soft_min_deg"] = 80
    elif failure == "timeout":
        now = 100
    svc._calibration_tick(now, obs, positions, limits)
    assert svc._calibration is None
    assert path.read_text() == "previous model"
    assert call("motion.calibration_lock", {"enabled": False}) in bus.publish.call_args_list
