"""Tests for StereoService — depth matching runs off the publisher thread."""
import threading
import time
from unittest.mock import MagicMock

import numpy as np

from src.core.bus import MessageBus
from src.services.stereo_service import StereoService


def _service(estimate):
    bus = MessageBus()
    vision = MagicMock()
    vision.latest_frame.return_value = np.zeros((480, 640, 3), np.uint8)
    cam2 = MagicMock()
    cam2.latest_frame.return_value = np.zeros((480, 640, 3), np.uint8)
    svc = StereoService(bus=bus, vision_service=vision, cam2_service=cam2)
    svc.on_start()
    svc._matcher = MagicMock()
    svc._matcher.estimate.side_effect = estimate
    return bus, svc


def _faces(n=1):
    return {"faces": [{"bbox": [100, 100, 200, 200], "centroid": [150, 150],
                       "face_id": f"id{i}", "depth_m": 1.0} for i in range(n)]}


def test_publish_does_not_wait_for_stereo_matching():
    release = threading.Event()
    bus, svc = _service(lambda *a: release.wait(2) and 1.5)
    try:
        start = time.monotonic()
        bus.publish("perception.faces", _faces())
        assert time.monotonic() - start < 0.2
    finally:
        release.set()
        svc.on_stop()


def test_worker_publishes_depth_for_latest_result():
    got = threading.Event()
    bus, svc = _service(lambda *a: 1.25)
    out = []
    bus.subscribe("vision.face_depth", lambda _t, p: (out.append(p), got.set()))
    try:
        bus.publish("perception.faces", _faces())
        assert got.wait(2)
        face = out[0]["faces"][0]
        assert face["method"] == "stereo" and face["depth_m"] == 1.25
    finally:
        svc.on_stop()
    assert svc._worker is None


def test_busy_worker_keeps_only_newest_result():
    release = threading.Event()
    seen = []
    def estimate(bbox, *_):
        seen.append(tuple(bbox))
        release.wait(2)
        return 1.0
    bus, svc = _service(estimate)
    try:
        bus.publish("perception.faces", _faces())
        deadline = time.monotonic() + 2
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
        for x in (10, 20, 30):
            bus.publish("perception.faces", {"faces": [{"bbox": [x, 0, x + 50, 50],
                                                        "centroid": [x, 25]}]})
        release.set()
        deadline = time.monotonic() + 2
        while len(seen) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        time.sleep(0.1)
        assert seen[1:] == [(30, 0, 80, 50)]
    finally:
        release.set()
        svc.on_stop()
