"""
Unit tests for src/vision/camera.py.

All tests run without hardware by patching picamera2 out — same
pattern as test_servo_controller.py.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Stub picamera2 so tests run on any machine (no Pi camera required)
# ---------------------------------------------------------------------------
_picamera2_stub = types.ModuleType("picamera2")


class _FakePicamera2:
    """Minimal Picamera2 stub for unit testing."""

    _instances: list = []

    def __init__(self, index: int = 0) -> None:
        self.index = index
        self._running = False
        self._config = None
        _FakePicamera2._instances.append(self)

    @staticmethod
    def global_camera_info():
        return [{"Num": 0, "Model": "imx708_wide"}]

    def create_video_configuration(self, main=None, controls=None):
        return {"type": "video", "main": main, "controls": controls}

    def create_still_configuration(self):
        return {"type": "still"}

    def configure(self, cfg):
        self._config = cfg

    def start(self):
        self._running = True

    def stop(self):
        self._running = False

    def close(self):
        pass

    def capture_array(self, stream="main"):
        h, w = 720, 1280
        return np.zeros((h, w, 3), dtype=np.uint8)

    def capture_file(self, path):
        pass


_picamera2_stub.Picamera2 = _FakePicamera2
sys.modules.setdefault("picamera2", _picamera2_stub)

# Now import the module under test (after stub is registered)
from src.vision.camera import Camera, CameraConfig  # noqa: E402


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCameraConfig:
    def test_defaults(self):
        cfg = CameraConfig()
        assert cfg.index == 0
        assert cfg.width == 1280
        assert cfg.height == 720
        assert cfg.framerate == 30
        assert cfg.stream_format == "RGB888"

    def test_custom_values(self):
        cfg = CameraConfig(index=1, width=640, height=480, framerate=60)
        assert cfg.index == 1
        assert cfg.width == 640
        assert cfg.height == 480
        assert cfg.framerate == 60


class TestCameraHardwareReady:
    def test_hardware_ready_with_stub(self):
        cam = Camera(CameraConfig(index=0))
        assert cam.hardware_ready is True

    def test_not_ready_when_index_missing(self):
        # index 99 won't be found in the stub's camera list
        cam = Camera(CameraConfig(index=99))
        assert cam.hardware_ready is False


class TestCameraLifecycle:
    def test_not_running_before_start(self):
        cam = Camera()
        assert cam.is_running is False

    def test_running_after_start(self):
        cam = Camera()
        cam.start()
        assert cam.is_running is True
        cam.close()

    def test_not_running_after_stop(self):
        cam = Camera()
        cam.start()
        cam.stop()
        assert cam.is_running is False

    def test_double_start_is_idempotent(self):
        cam = Camera()
        cam.start()
        cam.start()  # should not raise
        assert cam.is_running is True
        cam.close()

    def test_context_manager(self):
        with Camera() as cam:
            assert cam.is_running is True
        assert cam.is_running is False


class TestCaptureFrame:
    def test_raises_if_not_started(self):
        cam = Camera()
        with pytest.raises(RuntimeError, match="start()"):
            cam.capture_frame()

    def test_returns_numpy_array(self):
        with Camera() as cam:
            frame = cam.capture_frame()
        assert isinstance(frame, np.ndarray)

    def test_frame_shape(self):
        cfg = CameraConfig(width=1280, height=720)
        with Camera(cfg) as cam:
            frame = cam.capture_frame()
        assert frame.shape == (720, 1280, 3)

    def test_frame_dtype_uint8(self):
        with Camera() as cam:
            frame = cam.capture_frame()
        assert frame.dtype == np.uint8


class TestSimMode:
    def test_sim_mode_start_does_not_raise(self):
        cam = Camera(CameraConfig(index=99))  # forces sim
        cam.start()
        assert cam.is_running is True

    def test_sim_frame_shape(self):
        cfg = CameraConfig(index=99, width=640, height=480)
        cam = Camera(cfg)
        cam.start()
        frame = cam.capture_frame()
        assert frame.shape == (480, 640, 3)
        cam.close()

    def test_sim_capture_still_no_raise(self, tmp_path):
        cam = Camera(CameraConfig(index=99))
        cam.start()
        cam.capture_still(str(tmp_path / "test.jpg"))  # should not raise
        cam.close()
