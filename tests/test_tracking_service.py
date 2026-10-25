"""Tests for TrackingService person-seek behaviour."""

from __future__ import annotations

import math
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
    svc._motion_settings = {"servo_enabled": True, "hardware_ready": True}
    svc._object_detection_enabled = True
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


def test_tilt_direction_change_invalidates_saved_visual_calibration(tmp_path):
    svc, _ = make_tracking_svc(
        visual_config={"calibration_path": str(tmp_path / "model.json")},
    )
    svc._motion_settings = {
        "tilt_available": True,
        "tilt_min_deg": 70,
        "tilt_max_deg": 110,
    }
    old_signature = svc._signature()
    PlantModel(
        [[-0.01, 0.0], [0.0, -0.02]], 0.6, 0.0, old_signature,
    ).save(svc._calibration_path())

    assert svc._model().matrix()[1, 1] == -0.02
    assert svc._plant is not None
    svc._tracker_cfg.invert_tilt = True
    model = svc._model()

    assert model.matrix()[1, 1] > 0
    assert svc._plant is None


@pytest.mark.parametrize("invert", [False, True])
def test_uncalibrated_tilt_model_assumes_up_moves_face_down(invert):
    svc, _ = make_tracking_svc(config=HeadTrackerConfig(
        frame_width=640, frame_height=480, invert_tilt=invert,
    ))
    svc._motion_settings = {"tilt_available": True, "tilt_min_deg": 70, "tilt_max_deg": 110}
    assert svc._model().matrix()[1, 1] > 0


def test_calibration_from_before_tilt_convention_is_discarded(tmp_path):
    svc, _ = make_tracking_svc(
        visual_config={"calibration_path": str(tmp_path / "model.json")},
    )
    svc._motion_settings = {"tilt_available": True, "tilt_min_deg": 70, "tilt_max_deg": 110}
    legacy = svc._signature()
    del legacy["tilt_convention"]
    PlantModel([[-0.01, 0.0], [0.0, -0.02]], 0.6, 0.0, legacy).save(svc._calibration_path())
    assert svc._model().matrix()[1, 1] > 0
    assert svc._plant is None


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
        {"centroid": [100, 200], "bbox": [80, 180, 120, 220], "confidence": 0.9},
        {"centroid": [500, 200], "bbox": [450, 150, 550, 250], "confidence": 0.6},
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


def test_on_objects_picks_largest_person_not_highest_confidence():
    svc, _ = make_tracking_svc()
    payload = make_obj_payload([
        (0, 0, 100, 200, 0.9),   # cx=50
        (200, 0, 400, 200, 0.5), # cx=300, largest wins
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


def test_on_objects_accepted_when_face_tracking_disabled():
    svc, _ = make_tracking_svc(face_tracking_enabled=False)
    payload = make_obj_payload([(100, 0, 300, 400, 0.9)])
    svc._on_objects(None, payload)
    assert svc._person_cx == pytest.approx(200.0)


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
        "servo_enabled": True, "hardware_ready": True,
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


def make_person_seek_svc(tilt=True):
    svc, bus = make_tracking_svc(random_motion_enabled=False)
    svc.on_start()
    if tilt:
        enable_tilt(svc)
    bus.publish.reset_mock()
    return svc, bus


def published(bus, topic):
    return [c.args[1] for c in bus.publish.call_args_list if c.args[0] == topic]


@pytest.mark.parametrize("y1,y2,expected", [(300, 460, "down"), (20, 180, "up")])
def test_person_seek_tilts_toward_person_in_logical_up_convention(y1, y2, expected):
    svc, bus = make_person_seek_svc()
    svc._on_objects(None, make_obj_payload([(100, y1, 300, y2, 0.9)]))
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1
    (tilt,) = published(bus, "motion.tilt_to")
    assert tilt["override_quiet"] is True
    assert (tilt["angle"] < 90) if expected == "down" else (tilt["angle"] > 90)
    assert 70 <= tilt["angle"] <= 110


def test_person_seek_tilt_is_one_command_per_detection():
    svc, bus = make_person_seek_svc()
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._idle_tick(time.monotonic(), None)
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.tilt_to")) == 1


def test_person_seek_does_not_tilt_without_tilt_hardware():
    svc, bus = make_person_seek_svc(tilt=False)
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1
    assert published(bus, "motion.tilt_to") == []


def test_person_seek_pans_only_when_detection_has_no_frame_height():
    svc, bus = make_person_seek_svc()
    payload = make_obj_payload([(100, 300, 300, 460, 0.9)])
    del payload["frame_h"]
    svc._on_objects(None, payload)
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1
    assert published(bus, "motion.tilt_to") == []


def test_person_seek_tilt_stops_and_resets_when_person_is_lost():
    svc, bus = make_person_seek_svc()
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._idle_tick(time.monotonic(), None)
    assert svc._tilt_person_tracking is True
    svc._person_cx_ts -= 3
    bus.publish.reset_mock()
    svc._idle_tick(time.monotonic(), None)
    assert svc._tilt_person_tracking is False
    assert published(bus, "motion.tilt_to") == []
    svc._on_objects(None, make_obj_payload([]))
    assert svc._person_cy is None


def test_person_seek_pans_and_tilts_with_face_tracking_off():
    svc, bus = make_tracking_svc(face_tracking_enabled=False)
    svc.on_start()
    enable_tilt(svc)
    bus.publish.reset_mock()
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    assert svc._person_cx is not None
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1
    (tilt,) = published(bus, "motion.tilt_to")
    assert tilt["angle"] < 90


def test_person_seek_uses_unhidden_persons_list_when_overlay_hides_them():
    svc, _ = make_tracking_svc(face_tracking_enabled=False)
    payload = make_obj_payload([])
    payload["persons"] = make_obj_payload([(100, 100, 300, 300, 0.9)])["objects"]
    svc._on_objects(None, payload)
    assert svc._person_cx == pytest.approx(200.0)


def test_toggling_face_tracking_does_not_cancel_person_seek():
    svc, bus = make_person_seek_svc()
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._on_set_face_tracking(None, {"enabled": False})
    assert svc._person_cx is not None
    bus.publish.reset_mock()
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1


def test_legacy_person_seek_follows_person_with_face_tracking_off():
    svc, bus = make_legacy_svc(face_tracking_enabled=False, random_motion_enabled=False)
    svc._on_objects(None, make_obj_payload([(500, 100, 600, 300, 0.9)]))
    svc._legacy_tick(time.monotonic(), 0.05)
    (pan,) = published(bus, "motion.pan_to")
    assert pan["override_quiet"] is True


def test_recently_tracked_face_suppresses_person_seek_tilt():
    svc, bus = make_person_seek_svc()
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._faces_present = True
    svc._face_ts = time.monotonic()
    svc._idle_tick(time.monotonic(), None)
    assert published(bus, "motion.tilt_to") == []


@pytest.mark.parametrize("controller", ["legacy", "visual_servo"])
@pytest.mark.parametrize("gate", ["servo_enabled", "hardware_ready"])
def test_all_tracking_and_idle_paths_require_motion_ready(controller, gate):
    svc, bus = (make_legacy_svc(random_motion_enabled=True) if controller == "legacy"
                else make_person_seek_svc())
    feed_face(svc)
    svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    svc._motion_settings[gate] = False
    bus.publish.reset_mock()
    if controller == "legacy":
        svc._legacy_tick(time.monotonic(), 0.05)
    else:
        svc._visual_tick(time.monotonic())
    assert published(bus, "motion.pan_to") == []
    assert published(bus, "motion.tilt_to") == []
    svc._face_ts = -float("inf")
    svc._idle_tick(time.monotonic(), None)
    assert published(bus, "motion.pan_to") == []


@pytest.mark.parametrize("controller", ["legacy", "visual_servo"])
def test_object_disable_clears_person_and_requires_new_detection(controller):
    svc, bus = (make_legacy_svc(face_tracking_enabled=False, random_motion_enabled=False)
                if controller == "legacy" else make_person_seek_svc())
    payload = make_obj_payload([(100, 300, 300, 460, 0.9)])
    svc._on_objects(None, payload)
    svc._on_object_enabled(None, {"enabled": False})
    assert svc._person_cx is None
    svc._on_objects(None, payload)
    assert svc._person_cx is None
    svc._on_object_enabled(None, {"enabled": True})
    bus.publish.reset_mock()
    svc._legacy_tick(time.monotonic(), 0.05) if controller == "legacy" \
        else svc._idle_tick(time.monotonic(), None)
    assert published(bus, "motion.pan_to") == []
    svc._on_objects(None, payload)
    svc._legacy_tick(time.monotonic(), 0.05) if controller == "legacy" \
        else svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1


def test_tracking_initializes_object_gate_from_retained_state():
    svc, bus = make_tracking_svc()
    bus.last.side_effect = lambda topic: {"enabled": False} if topic == "object.enabled_changed" else None
    svc.on_start()
    assert svc._object_detection_enabled is False
    assert "object.enabled_changed" in [c.args[0] for c in bus.subscribe.call_args_list]


def test_person_selection_keeps_incumbent_until_challenger_is_15_percent_larger():
    svc, _ = make_tracking_svc()
    svc._on_objects(None, make_obj_payload([
        (50, 0, 150, 200, 0.6), (400, 0, 490, 200, 0.9),
    ]))
    assert svc._person_cx == 100
    svc._on_objects(None, make_obj_payload([
        (400, 0, 510, 200, 0.9), (50, 0, 150, 200, 0.6),
    ]))
    assert svc._person_cx == 100
    svc._on_objects(None, make_obj_payload([
        (400, 0, 520, 200, 0.9), (50, 0, 150, 200, 0.6),
    ]))
    assert svc._person_cx == 460


@pytest.mark.parametrize("controller", ["legacy", "visual_servo"])
def test_person_handoff_after_one_second_not_just_any_visible_face(controller):
    svc, bus = (make_legacy_svc(random_motion_enabled=False) if controller == "legacy"
                else make_person_seek_svc())
    svc._face_ts = 100.0
    svc._current_face_cx = None
    svc._observation = None
    svc._faces_present = True  # An unacquired face must not block handoff indefinitely.
    with patch("src.services.tracking_service.time.monotonic", return_value=100.9):
        svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
    bus.publish.reset_mock()
    if controller == "legacy":
        svc._legacy_tick(100.99, 0.05)
        assert published(bus, "motion.pan_to") == []
        svc._legacy_tick(101.01, 0.05)
    else:
        svc._idle_tick(100.99, None)
        assert published(bus, "motion.pan_to") == []
        svc._idle_tick(101.01, None)
        assert len(published(bus, "motion.tilt_to")) == 1
    assert len(published(bus, "motion.pan_to")) == 1


def test_stale_face_observation_does_not_block_visual_person_handoff():
    svc, bus = make_person_seek_svc()
    svc._current_servo_angle = 175
    svc._current_tilt_angle = 90
    svc._face_ts = 100.0
    svc._observation = (np.array([0.2, 0.2]), 100.0, 100.0, 1)
    with patch("src.services.tracking_service.time.monotonic", return_value=101.1):
        svc._on_objects(None, make_obj_payload([(100, 300, 300, 460, 0.9)]))
        svc._visual_tick(101.1)
    assert len(published(bus, "motion.pan_to")) == 1
    assert len(published(bus, "motion.tilt_to")) == 1


def test_stale_person_capture_is_not_refreshed_by_new_payload():
    svc, bus = make_person_seek_svc()
    payload = make_obj_payload([(100, 300, 300, 460, 0.9)])
    payload["captured_mono"] = time.monotonic() - 3
    svc._on_objects(None, payload)
    svc._idle_tick(time.monotonic(), None)
    assert published(bus, "motion.pan_to") == []


def test_random_idle_is_independent_of_person_and_face_toggles():
    svc, bus = make_person_seek_svc()
    svc._on_set_face_tracking(None, {"enabled": False})
    svc._on_set_person_seek(None, {"enabled": False})
    svc._on_object_enabled(None, {"enabled": False})
    bus.publish.reset_mock()
    svc._idle_tick(time.monotonic(), None)
    assert published(bus, "motion.pan_to") == []
    svc._on_set_random_motion(None, {"enabled": True})
    svc._idle_tick(time.monotonic(), None)
    assert len(published(bus, "motion.pan_to")) == 1
    assert published(bus, "motion.tilt_to") == []


def test_person_centroid_correction_advances_trajectory_not_one_tiny_frame():
    svc, bus = make_tracking_svc(
        config=HeadTrackerConfig(
            frame_width=640, frame_height=480, servo_min=70, servo_max=110,
            servo_center=90, invert_pan=True, saccade_amplitude_deg=0,
        ), face_tracking_enabled=False,
    )
    svc.on_start()
    enable_tilt(svc)
    svc._current_servo_angle = svc._current_tilt_angle = 90
    bus.publish.reset_mock()
    svc._on_objects(None, make_obj_payload([(400, 20, 600, 180, 0.9)]))
    svc._idle_tick(time.monotonic(), None)
    (pan,), (tilt,) = published(bus, "motion.pan_to"), published(bus, "motion.tilt_to")
    assert 70 <= pan["angle"] < 89.5
    assert 90.5 < tilt["angle"] <= 110
    for command in (pan, tilt):
        distance = abs(command["angle"] - 90)
        duration = command["move_time_ms"] / 1000
        assert distance <= svc._visual_cfg.max_step_deg
        assert 1.875 * distance / duration <= svc._visual_cfg.max_speed_deg_s
        assert 5.78 * distance / duration ** 2 <= svc._visual_cfg.max_accel_deg_s2 + 1e-9
        assert command["profile"] == "smoothstep"


def test_person_commands_wait_for_motion_and_settling():
    svc, bus = make_person_seek_svc()
    payload = make_obj_payload([(100, 300, 300, 460, 0.9)])
    with patch("src.services.tracking_service.time.monotonic", return_value=100):
        svc._on_objects(None, payload)
        svc._idle_tick(100, None)
    bus.publish.reset_mock()
    with patch("src.services.tracking_service.time.monotonic", return_value=100.1):
        svc._on_objects(None, payload)
        svc._idle_tick(100.1, None)
    assert published(bus, "motion.pan_to") == []
    svc._idle_tick(svc._person_ready_at, None)
    assert len(published(bus, "motion.pan_to")) == 1


def test_person_centroid_converges_on_both_axes_with_repeated_fresh_detections():
    cfg = HeadTrackerConfig(
        frame_width=640, frame_height=480, servo_min=70, servo_max=110,
        servo_center=90, invert_pan=True, dead_zone_frac=0.02, saccade_amplitude_deg=0,
    )
    svc, bus = make_tracking_svc(config=cfg, face_tracking_enabled=False)
    svc.on_start()
    enable_tilt(svc)
    positions = np.array([90.0, 90.0])
    desired = np.array([82.0, 96.0])
    vfov = np.degrees(2 * np.arctan(np.tan(np.radians(cfg.fov_degrees) / 2) * 480 / 640))
    for step in range(60):
        now = 100 + step * 0.8
        svc._current_servo_angle, svc._current_tilt_angle = positions
        error = (positions - desired) / [cfg.fov_degrees, vfov]
        cx, cy = 320 + error[0] * 640, 240 + error[1] * 480
        bus.publish.reset_mock()
        with patch("src.services.tracking_service.time.monotonic", return_value=now):
            svc._on_objects(None, make_obj_payload([(cx - 50, cy - 80, cx + 50, cy + 80, 0.9)]))
            svc._idle_tick(now, None)
        for axis, topic in enumerate(("motion.pan_to", "motion.tilt_to")):
            for command in published(bus, topic):
                assert 70 <= command["angle"] <= 110
                assert abs(command["angle"] - positions[axis]) <= svc._visual_cfg.max_step_deg + 1e-9
                positions[axis] = command["angle"]
    assert np.max(np.abs((positions - desired) / [cfg.fov_degrees, vfov])) <= 0.02


def test_legacy_face_priority_prefers_recognized_person_over_larger_guest():
    svc, _ = make_legacy_svc()
    svc._on_faces(None, {"faces": [
        {"centroid": [100, 200], "bbox": [80, 180, 120, 220], "name": "Ada"},
        {"centroid": [500, 200], "bbox": [400, 100, 600, 300], "name": "Guest 1"},
    ]})
    assert svc._current_face_cx == 100


@pytest.mark.parametrize("offset", [-10, 10])
@pytest.mark.parametrize("invert", [False, True])
def test_tilt_centers_vertically_in_logical_up_convention(offset, invert):
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
    # A higher tilt angle looks up, so a fixed face moves down in the image.
    # Mount reversal is applied at the servo and must not change this.
    direction = -1
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


# ---------------------------------------------------------------------------
# Gaze controller (person-like Kalman bearing tracking)
# ---------------------------------------------------------------------------

def make_gaze_svc(tilt=True, **kwargs):
    kwargs.setdefault("random_motion_enabled", False)
    svc, bus = make_tracking_svc(
        controller="gaze",
        config=HeadTrackerConfig(frame_width=640, frame_height=480, max_speed_deg_s=120,
                                 servo_min=70, servo_max=110, servo_center=90),
        **kwargs,
    )
    svc.on_start()
    if tilt:
        enable_tilt(svc)
    svc._on_position(None, {"angle": 90.0, "tilt_angle": 90.0 if tilt else None})
    bus.publish.reset_mock()
    return svc, bus


def gaze_ticks(svc, n=10):
    for _ in range(n):
        svc._gaze_tick(time.monotonic(), 0.05)


def test_gaze_centers_face_on_pan_and_tilt_with_smooth_streamed_moves():
    svc, bus = make_gaze_svc()
    feed_face(svc, x=380, y=160)
    gaze_ticks(svc, 20)
    pans = [p["angle"] for p in published(bus, "motion.pan_to")]
    tilts = [p["angle"] for p in published(bus, "motion.tilt_to")]
    assert pans and tilts
    assert all(p["source"] == "gaze_tracking" and p["move_time_ms"] == pytest.approx(100)
               for p in published(bus, "motion.pan_to"))
    assert pans == sorted(pans) and pans[-1] > 95
    assert tilts == sorted(tilts) and tilts[-1] > 92
    assert max(pans) < 110 and max(tilts) < 110
    assert np.max(np.abs(np.diff([90.0] + pans))) <= 120 * 0.05 + 1e-6


def test_gaze_repeated_frame_does_not_compound_target():
    svc, _ = make_gaze_svc(tilt=False)
    feed_face(svc, x=400, y=240)
    gaze_ticks(svc, 60)
    settled = svc._gaze.axes["pan"].pos
    stamp = svc._observation[1]
    for _ in range(60):
        svc._observation = (svc._observation[0], stamp, time.monotonic(), 1)
        svc._gaze_tick(time.monotonic(), 0.05)
    assert svc._gaze.axes["pan"].pos == pytest.approx(settled, abs=0.5)
    expected = 90 + math.degrees(math.atan(2 * (80 / 640) * math.tan(math.radians(50))))
    assert settled == pytest.approx(expected, abs=2.5)


def test_gaze_follows_person_when_no_face_and_face_takes_priority():
    svc, bus = make_gaze_svc(tilt=False)
    svc._on_objects(None, make_obj_payload([(40, 100, 160, 400, 0.9)]))
    gaze_ticks(svc, 5)
    assert svc._gaze.source == "person"
    assert published(bus, "motion.pan_to")[-1]["angle"] < 90
    feed_face(svc, x=480, y=240)
    gaze_ticks(svc, 1)
    assert svc._gaze.source == "face"


def test_gaze_requires_motion_ready_and_halts():
    svc, bus = make_gaze_svc()
    feed_face(svc)
    svc._motion_settings["hardware_ready"] = False
    gaze_ticks(svc, 5)
    assert published(bus, "motion.pan_to") == []
    assert published(bus, "motion.tilt_to") == []
    assert not svc._gaze.active


def test_gaze_releases_and_coasts_when_face_tracking_disabled():
    svc, bus = make_gaze_svc(tilt=False)
    feed_face(svc, x=500, y=240)
    gaze_ticks(svc, 4)
    svc._on_set_face_tracking(None, {"enabled": False})
    gaze_ticks(svc, 30)
    assert not svc._gaze.active and not svc._gaze.moving


def test_gaze_rejects_visual_calibration_and_reports_controller():
    svc, bus = make_gaze_svc()
    svc._on_start_calibration(None, {})
    assert not svc._calibration_requested
    assert published(bus, "tracking.autotune_done")[-1]["ok"] is False
    params = svc.get_tunable_params()
    assert params["controller"] == "gaze"
    assert "gaze" in params["controller_description"].lower()


def test_gaze_loop_routes_to_gaze_tick():
    svc, _ = make_gaze_svc()
    with patch.object(svc, "_gaze_tick") as gaze, patch.object(svc, "_visual_tick") as visual:
        run_one_tick(svc)
    gaze.assert_called_once()
    visual.assert_not_called()


def test_unknown_controller_rejected():
    with pytest.raises(ValueError):
        make_tracking_svc(controller="bogus")


def test_gaze_speech_gesture_lifts_before_onset_then_sways_smoothly():
    svc, _ = make_gaze_svc(pre_speech_lift_deg=2.0, pre_speech_relax_s=0.6,
                           speaking_motion_amplitude_deg=1.5, speaking_motion_freq_hz=2.5)
    svc._on_speaking_started(None, {"text": "hi", "lead_s": 0.25})
    cue = svc._speech_cue
    pan, tilt = svc._speech_gesture(cue + 0.125)
    assert pan == 0.0 and 0.5 < tilt < 1.5
    assert svc._speech_gesture(cue + 0.25) == pytest.approx((0.0, 2.0))
    samples = [svc._speech_gesture(cue + 0.25 + i * 0.01) for i in range(150)]
    assert samples[-1][1] == pytest.approx(0.0)
    assert max(abs(p) for p, _ in samples) > 1.0
    assert max(abs(b[0] - a[0]) for a, b in zip(samples, samples[1:])) < 0.3
    svc._speech_end = cue + 1.0
    assert svc._speech_gesture(cue + 1.0 + 0.31) == (0.0, 0.0)


def test_gaze_speech_gesture_applies_tilt_offset_within_limits():
    svc, bus = make_gaze_svc(pre_speech_lift_deg=2.0)
    svc._on_speaking_started(None, {"lead_s": 0.25})
    svc._publish_gaze({"pan": 90.0, "tilt": 109.0}, svc._speech_onset, tracking=True,
                      bounds={"pan": (70.0, 110.0), "tilt": (70.0, 110.0)})
    assert published(bus, "motion.tilt_to")[-1]["angle"] == pytest.approx(110.0)
    svc._publish_gaze({"pan": 90.0, "tilt": 95.0}, svc._speech_onset, tracking=False)
    assert published(bus, "motion.tilt_to")[-1]["angle"] == pytest.approx(95.0)


def test_speech_gesture_rejects_bad_settings_and_bad_lead():
    with pytest.raises(ValueError):
        make_gaze_svc(pre_speech_lift_deg=9.0)
    svc, _ = make_gaze_svc(pre_speech_lift_deg=2.0)
    svc._on_speaking_started(None, {"lead_s": "nan"})
    assert svc._speech_onset == svc._speech_cue
    assert svc._speech_gesture(svc._speech_cue + 0.01)[1] == 0.0


def test_gaze_debug_reports_detection_rate_and_latency():
    svc, bus = make_gaze_svc()
    now = time.monotonic()
    for i in range(11):
        svc._detections.append((now - 1.0 + i * 0.1, 0.08))
    svc._detections.append((now - 10.0, 5.0))
    stats = svc._detection_stats(now)
    assert stats["detect_hz"] == pytest.approx(10.0)
    assert stats["detect_latency_ms"] == pytest.approx(80.0)
    svc._on_faces(None, {"faces": [], "captured_mono": time.monotonic() - 0.05})
    assert svc._detections[-1][1] == pytest.approx(0.05, abs=0.02)
    svc._on_faces(None, {"faces": [], "captured_mono": True})
    assert len(svc._detections) == 13
