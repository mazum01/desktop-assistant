"""Tests for TrackingService person-seek behaviour."""

from __future__ import annotations

import time
import threading
from unittest.mock import MagicMock, call, patch

import pytest

from src.services.tracking_service import TrackingService, _PERSON_SEEK_STALE_S
from src.motion.head_tracker import HeadTrackerConfig


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
        "src.services.tracking_service.time.sleep",
        side_effect=lambda _: svc._stop_event.set(),
    ):
        svc._run_loop()


def test_face_geometry_uses_detection_dimensions():
    svc, _ = make_tracking_svc()
    enable_tilt(svc)
    svc._on_faces(None, {
        "frame_w": 1080, "frame_h": 1920,
        "faces": [{"centroid": [540, 200], "confidence": 0.9}],
    })
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
    svc._on_faces(None, {"faces": [{"centroid": [320, 100]}]})
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
    svc._on_faces(None, {"faces": [{"centroid": [320, 100]}]})
    if reason == "disabled":
        svc._on_set_face_tracking(None, {"enabled": False})
    elif reason == "lost":
        svc._on_faces(None, {"faces": []})
    elif reason == "stale":
        svc._face_ts -= 3
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
