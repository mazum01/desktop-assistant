"""
Unit tests for src/services/raw_camera_service.py.

Runs without hardware by mocking the Camera dependency.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.core.bus import MessageBus
from src.services.raw_camera_service import RawCameraService, RawCameraConfig


def _make_service(framerate: int = 15) -> tuple[RawCameraService, MessageBus]:
    bus = MessageBus()
    svc = RawCameraService(
        bus=bus,
        camera_config=RawCameraConfig(
            index=1, width=320, height=240, framerate=framerate
        ),
    )
    return svc, bus


class TestRawCameraServiceInit:
    def test_tick_rate_derived_from_framerate(self):
        svc, _ = _make_service(framerate=10)
        assert abs(svc.tick_seconds - 0.1) < 1e-9

    def test_defaults_to_no_hardware_ready(self):
        svc, _ = _make_service()
        assert not svc.hardware_ready

    def test_latest_jpeg_none_before_first_tick(self):
        svc, _ = _make_service()
        assert svc.latest_jpeg() is None

    def test_focus_retained_after_resolution_change(self):
        svc, _ = _make_service()
        svc._camera = MagicMock()
        svc._camera.set_focus.return_value = {"af_mode": "manual", "lens_position": 2.}
        assert svc.set_focus("manual", 2.)["lens_position"] == 2.
        svc._on_set_resolution("camera2.set_resolution", {"width": 1920, "height": 1080})
        assert svc._cam_cfg.af_mode == "manual"
        assert svc._cam_cfg.lens_position == 2.

    def test_focus_failure_does_not_update_config(self):
        svc, _ = _make_service()
        svc._camera = MagicMock()
        svc._camera.set_focus.side_effect = RuntimeError("focus failed")
        with pytest.raises(RuntimeError, match="focus failed"):
            svc.set_focus("manual", 2.)
        assert svc._cam_cfg.af_mode == "continuous"


class TestRawCameraServiceTick:
    def test_tick_encodes_and_stores_jpeg(self):
        svc, bus = _make_service()

        fake_cam = MagicMock()
        fake_cam.hardware_ready = True
        fake_cam.capture_frame.return_value = np.zeros((240, 320, 3), dtype=np.uint8)
        svc._camera = fake_cam
        svc._running = True

        published = []
        bus.subscribe("vision.frame2_ready", lambda t, p: published.append(p))

        svc.run_tick()

        assert svc.latest_jpeg() is not None
        assert len(published) == 1
        assert published[0]["index"] == 1

    def test_tick_increments_index(self):
        svc, bus = _make_service()
        fake_cam = MagicMock()
        fake_cam.hardware_ready = True
        fake_cam.capture_frame.return_value = np.zeros((240, 320, 3), dtype=np.uint8)
        svc._camera = fake_cam
        svc._running = True

        indices = []
        bus.subscribe("vision.frame2_ready", lambda t, p: indices.append(p["index"]))

        svc.run_tick()
        svc.run_tick()

        assert indices == [1, 2]

    def test_tick_handles_capture_error_gracefully(self):
        svc, _ = _make_service()
        fake_cam = MagicMock()
        fake_cam.capture_frame.side_effect = RuntimeError("no frame")
        svc._camera = fake_cam
        svc._running = True

        # Should not raise
        svc.run_tick()
        assert svc.latest_jpeg() is None
