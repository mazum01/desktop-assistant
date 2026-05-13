"""Tests for TrackingService person-seek behaviour."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, call

import pytest

from src.services.tracking_service import TrackingService, _PERSON_SEEK_STALE_S


def make_tracking_svc(**kwargs) -> tuple[TrackingService, MagicMock]:
    bus = MagicMock()
    bus.subscribe.return_value = lambda: None
    bus.last.return_value = None
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
