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

    def create_video_configuration(self, main=None, controls=None, buffer_count=None,
                                   transform=None):
        return {"type": "video", "main": main, "controls": controls, "transform": transform}

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
    def test_observation_shares_read_only_frame_with_availability_timestamp(self):
        import threading
        cam = Camera.__new__(Camera)
        cam._sim = False
        cam._running = True
        cam._frame_lock = threading.Lock()
        cam._latest_array = np.zeros((10, 20, 3), dtype=np.uint8)
        cam._latest_array.flags.writeable = False
        cam._latest_frame_monotonic = 123.0
        frame, stamp = cam.capture_observation()
        assert stamp == 123.0
        assert frame is cam._latest_array
        with pytest.raises(ValueError):
            frame[:] = 1

    def test_defaults(self):
        cfg = CameraConfig()
        assert cfg.index == 0
        assert cfg.width == 640
        assert cfg.height == 480
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


class TestCameraSensorRotation:
    def _camera(self, rotation_deg):
        cam = Camera(CameraConfig(rotation_deg=rotation_deg))
        cam._sim = False
        cam._cam = MagicMock()
        return cam

    @staticmethod
    def _patched(cam):
        def deliver():
            cam._latest_array = np.zeros((2, 2, 3), dtype=np.uint8)
        return patch.object(cam, "_capture_loop", side_effect=deliver)

    def _start(self, cam):
        with self._patched(cam), patch("src.vision.camera.time.sleep"):
            cam.start()

    def test_half_turn_uses_sensor_flip(self):
        cam = self._camera(180)
        self._start(cam)
        transform = cam._cam.create_video_configuration.call_args.kwargs["transform"]
        assert transform.hflip and transform.vflip
        assert cam.hw_rotation_deg == 180
        cam.close()

    @pytest.mark.parametrize("deg", [0, 90, 270, 45])
    def test_other_angles_stay_in_software(self, deg):
        cam = self._camera(deg)
        self._start(cam)
        assert "transform" not in cam._cam.create_video_configuration.call_args.kwargs
        assert cam.hw_rotation_deg == 0
        cam.close()

    def test_flip_failure_falls_back_to_software(self):
        cam = self._camera(180)
        cam._cam.configure.side_effect = [RuntimeError("no flip"), None]
        self._start(cam)
        assert cam.hw_rotation_deg == 0
        assert cam.is_running
        cam.close()

    def test_set_rotation_reconfigures_only_when_flip_changes(self):
        cam = self._camera(0)
        self._start(cam)
        with self._patched(cam), patch("src.vision.camera.time.sleep"):
            cam.set_rotation(90)
            assert cam._cam.configure.call_count == 1
            cam.set_rotation(180)
        assert cam._cam.configure.call_count == 2
        assert cam.hw_rotation_deg == 180
        assert cam.config.rotation_deg == 180
        cam.close()


class TestCameraIspSmallFrame:
    def test_detector_input_size(self):
        from src.vision.camera import detector_input_size
        assert detector_input_size(1920, 1080) == (640, 360)
        assert detector_input_size(1280, 720) == (640, 360)
        assert detector_input_size(640, 480) is None
        assert detector_input_size(0, 0) is None

    def _start(self, cam):
        def deliver():
            cam._latest_array = np.zeros((2, 2, 3), dtype=np.uint8)
        with patch.object(cam, "_capture_loop", side_effect=deliver), \
                patch("src.vision.camera.time.sleep"):
            cam.start()

    def test_requests_lores_stream_sized_for_detectors(self):
        cam = Camera(CameraConfig(width=1920, height=1080, rotation_deg=180))
        cam._sim = False
        cam._cam = MagicMock()
        self._start(cam)
        kwargs = cam._cam.create_video_configuration.call_args.kwargs
        assert kwargs["lores"] == {"size": (640, 360), "format": "RGB888"}
        assert cam.small_size == (640, 360)
        assert cam.hw_rotation_deg == 180
        cam.close()

    def test_lores_failure_keeps_sensor_flip(self):
        cam = Camera(CameraConfig(width=1920, height=1080, rotation_deg=180))
        cam._sim = False
        cam._cam = MagicMock()
        cam._cam.configure.side_effect = [RuntimeError("no lores"), None]
        self._start(cam)
        last = cam._cam.create_video_configuration.call_args.kwargs
        assert "lores" not in last and "transform" in last
        assert cam.small_size is None and cam.hw_rotation_deg == 180
        cam.close()

    def test_capture_loop_stores_matched_read_only_pair(self):
        cam = Camera(CameraConfig(width=1920, height=1080))
        cam._sim = False
        cam._running = True
        cam._small_size = (640, 360)
        main = np.zeros((1080, 1920, 3), np.uint8)
        small = np.zeros((360, 640, 3), np.uint8)
        cam._cam = MagicMock()

        def capture_arrays(names):
            assert names == ["main", "lores"]
            calls.append(1)
            if len(calls) > 1:
                cam._running = False
            return [main, small], {}
        calls = []
        cam._cam.capture_arrays.side_effect = capture_arrays
        cam._capture_loop()
        cam._running = True
        frame, got_small, _ = cam.capture_frames()
        assert frame is main and got_small is small
        assert not frame.flags.writeable and not got_small.flags.writeable


class TestCameraLifecycle:
    def test_manual_infinity_applied_on_start(self):
        cam = Camera(CameraConfig(af_mode="manual", lens_position=0.))
        cam._sim = False
        cam._cam = MagicMock()
        with patch.object(cam, "_capture_loop"), patch("src.vision.camera.time.sleep"):
            cam.start()
        controls = cam._cam.create_video_configuration.call_args.kwargs["controls"]
        assert controls["AfMode"] == 0
        assert controls["LensPosition"] == 0.
        cam.close()

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


class TestFocusControls:
    @pytest.fixture
    def camera(self):
        cam = Camera(CameraConfig())
        cam._sim = False
        cam._running = True
        cam._cam = MagicMock()
        cam._cam.camera_controls = {"AfMode": (0, 2, 0), "LensPosition": (0., 15., 1.)}
        return cam

    @pytest.mark.parametrize("mode,position,controls", [
        ("manual", 0., {"AfMode": 0, "LensPosition": 0.}),
        ("manual", 2., {"AfMode": 0, "LensPosition": 2.}),
        ("continuous", 2., {"AfMode": 2, "AfSpeed": 1}),
        ("auto", 2., {"AfMode": 1, "AfTrigger": 0}),
        ("off", 0., {"AfMode": 0, "LensPosition": 0.}),
    ])
    def test_applies_focus_and_retains_config(self, camera, mode, position, controls):
        result = camera.set_focus(mode, position)
        camera._cam.set_controls.assert_called_once_with(controls)
        assert result["af_mode"] == mode
        assert result["lens_position"] == position
        assert result["supported"]
        assert result["max_position"] == 15.
        camera._sim = True
        camera.set_resolution(1920, 1080)
        assert camera.config.af_mode == mode
        assert camera.config.lens_position == position

    @pytest.mark.parametrize("value", [-1., 16., float("nan"), float("inf"), "2", True, None])
    def test_rejects_invalid_position_without_hardware_write(self, camera, value):
        with pytest.raises(ValueError):
            camera.set_focus("manual", value)
        camera._cam.set_controls.assert_not_called()

    def test_rejects_invalid_mode(self, camera):
        with pytest.raises(ValueError):
            camera.set_focus("invalid", 0.)
        camera._cam.set_controls.assert_not_called()

    def test_surfaces_hardware_failure_without_updating_config(self, camera):
        camera._cam.set_controls.side_effect = RuntimeError("actuator failed")
        with pytest.raises(RuntimeError, match="actuator failed"):
            camera.set_focus("manual", 2.)
        assert camera.config.af_mode == "continuous"

    def test_fixed_focus_camera_is_unsupported(self, camera):
        camera._cam.camera_controls = {}
        assert not camera.get_focus()["supported"]
        with pytest.raises(RuntimeError, match="unavailable"):
            camera.set_focus("manual", 0.)

    def test_simulated_camera_is_unsupported(self):
        camera = Camera(CameraConfig(index=99))
        assert not camera.get_focus()["supported"]
        with pytest.raises(RuntimeError, match="unavailable"):
            camera.set_focus("manual", 0.)
