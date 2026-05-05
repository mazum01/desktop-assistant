"""Unit tests for src/perception/."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.core.bus import MessageBus
from src.perception.hailo_inference import HailoInference
from src.perception.face_detector import FaceDetection, FaceDetector
from src.services.perception_service import PerceptionConfig, PerceptionService


# ── HailoInference ───────────────────────────────────────────────────────────

class TestHailoInference:
    def test_sim_mode_when_hef_missing(self):
        eng = HailoInference("/nonexistent/model.hef")
        assert eng.hardware_ready is False
        result = eng.infer({"input_layer1": np.zeros((640, 640, 3), dtype=np.uint8)})
        assert result == {}

    def test_sim_mode_when_hailo_unavailable(self):
        with patch("src.perception.hailo_inference._HAILO_AVAILABLE", False):
            eng = HailoInference("/any/path.hef")
        assert eng.hardware_ready is False

    def test_context_manager_closes_cleanly(self):
        with HailoInference("/nonexistent/model.hef") as eng:
            assert eng.hardware_ready is False
        # Should not raise


# ── FaceDetector ─────────────────────────────────────────────────────────────

def _make_sim_detector() -> FaceDetector:
    """Return a FaceDetector forced into sim mode."""
    det = FaceDetector.__new__(FaceDetector)
    det._conf_thr = 0.45
    det._nms_thr = 0.4
    det._engine = None
    det._haar = None
    det._backend = "sim"
    return det


class TestFaceDetector:
    def test_sim_mode_returns_empty(self):
        det = _make_sim_detector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        assert det.detect(frame) == []

    def test_hardware_ready_false_in_sim(self):
        det = _make_sim_detector()
        assert det.hardware_ready is False
        assert det.backend == "sim"

    def test_face_detection_dataclass(self):
        fd = FaceDetection(
            bbox=(10, 20, 110, 120),
            confidence=0.95,
            centroid=(60, 70),
        )
        assert fd.centroid == (60, 70)
        assert fd.landmarks is None

    def test_preprocess_returns_640x640(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        blob, sx, sy, px, py = FaceDetector._preprocess(frame)
        assert blob.shape == (640, 640, 3)
        assert blob.dtype == np.uint8

    def test_preprocess_letterbox_square_input(self):
        frame = np.zeros((480, 480, 3), dtype=np.uint8)
        blob, sx, sy, px, py = FaceDetector._preprocess(frame)
        assert blob.shape == (640, 640, 3)

    def test_cpu_fallback_initialises(self):
        """FaceDetector should find the OpenCV Haar cascade and use CPU backend."""
        with patch("src.perception.face_detector.FaceDetector._find_hef", return_value=None):
            det = FaceDetector()
        assert det.backend in ("cpu", "sim")  # sim if no display/cascade in CI

    def test_hailo_backend_uses_engine(self):
        """When HailoInference.hardware_ready is True, backend should be hailo."""
        mock_engine = MagicMock()
        mock_engine.hardware_ready = True
        mock_engine.infer.return_value = {}  # empty outputs → no detections
        with patch("src.perception.face_detector.HailoInference", return_value=mock_engine), \
             patch("src.perception.face_detector.FaceDetector._find_hef", return_value="/fake.hef"):
            det = FaceDetector()
        assert det.backend == "hailo"
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        result = det.detect(frame)
        assert result == []
        assert mock_engine.infer.called


# ── PerceptionService ─────────────────────────────────────────────────────────

def _make_perception_svc(bus, faces=None, backend="sim"):
    """Build PerceptionService with a mocked detector and vision service."""
    mock_det = MagicMock()
    mock_det.backend = backend
    mock_det.detect.return_value = faces or []

    mock_vis = MagicMock()
    mock_vis.latest_frame.return_value = np.zeros((480, 640, 3), dtype=np.uint8)

    cfg = PerceptionConfig(max_fps=1000)  # no rate limiting in tests
    svc = PerceptionService(bus=bus, vision_service=mock_vis, detector=mock_det, config=cfg)
    return svc, mock_det, mock_vis


class TestPerceptionService:
    def test_publishes_empty_faces_when_none_detected(self):
        bus = MessageBus()
        svc, _, _ = _make_perception_svc(bus, faces=[])
        svc.start()
        received = []
        bus.subscribe("perception.faces", lambda t, p: received.append(p))
        try:
            bus.publish("vision.frame_ready", {"index": 1})
            time.sleep(0.05)
            assert len(received) >= 1
            assert received[0]["count"] == 0
        finally:
            svc.stop()

    def test_publishes_face_data_when_detected(self):
        bus = MessageBus()
        face = FaceDetection(bbox=(10, 20, 110, 120), confidence=0.9, centroid=(60, 70))
        svc, _, _ = _make_perception_svc(bus, faces=[face])
        svc.start()
        received = []
        bus.subscribe("perception.faces", lambda t, p: received.append(p))
        try:
            bus.publish("vision.frame_ready", {"index": 1})
            time.sleep(0.05)
            assert received[0]["count"] == 1
            assert received[0]["faces"][0]["confidence"] == 0.9
            assert received[0]["faces"][0]["centroid"] == [60, 70]
        finally:
            svc.stop()

    def test_rate_limiting_skips_rapid_frames(self):
        bus = MessageBus()
        cfg = PerceptionConfig(max_fps=1.0)  # max 1 detection/sec
        mock_det = MagicMock()
        mock_det.backend = "sim"
        mock_det.detect.return_value = []
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = PerceptionService(bus=bus, vision_service=mock_vis, detector=mock_det, config=cfg)
        svc.start()
        try:
            # Fire 5 frames in rapid succession
            for _ in range(5):
                bus.publish("vision.frame_ready", {"index": 1})
            time.sleep(0.05)
            # Only 1 detection should have run due to rate cap
            assert mock_det.detect.call_count <= 1
        finally:
            svc.stop()

    def test_publishes_error_on_detect_exception(self):
        bus = MessageBus()
        mock_det = MagicMock()
        mock_det.backend = "sim"
        mock_det.detect.side_effect = RuntimeError("boom")
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
        cfg = PerceptionConfig(max_fps=1000)
        svc = PerceptionService(bus=bus, vision_service=mock_vis, detector=mock_det, config=cfg)
        svc.start()
        errors = []
        bus.subscribe("perception.error", lambda t, p: errors.append(p))
        try:
            bus.publish("vision.frame_ready", {"index": 1})
            time.sleep(0.05)
            assert len(errors) >= 1
            assert "detect_failed" in errors[0]["reason"]
        finally:
            svc.stop()

    def test_no_frame_without_vision_service(self):
        bus = MessageBus()
        mock_det = MagicMock()
        mock_det.backend = "sim"
        cfg = PerceptionConfig(max_fps=1000)
        svc = PerceptionService(bus=bus, vision_service=None, detector=mock_det, config=cfg)
        svc.start()
        try:
            bus.publish("vision.frame_ready", {"index": 1})
            time.sleep(0.05)
            mock_det.detect.assert_not_called()
        finally:
            svc.stop()
