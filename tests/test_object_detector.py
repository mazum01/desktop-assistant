"""
Tests for ObjectDetector (sim mode — no Hailo device required).
"""
import numpy as np
import pytest

from src.perception.object_detector import ObjectDetector, COCO_CLASSES, Detection


@pytest.fixture
def sim_detector():
    d = ObjectDetector.__new__(ObjectDetector)
    d._conf_threshold = 0.4
    d._min_box_area = 400
    d._iou_threshold = 0.55
    d._class_thresholds = {**{k: 0.25 for k in COCO_CLASSES}, "person": 0.35}
    d._engine = None
    d._sim = True
    d._backend = "sim"
    d._letterbox_buf = np.zeros((640, 640, 3), dtype=np.uint8)
    d._lb_src_shape = (0, 0)
    d._lb_scale = 1.0
    d._lb_new_w = 640
    d._lb_new_h = 640
    d._lb_pad_top = 0
    d._lb_pad_left = 0
    return d


def test_coco_classes_count():
    assert len(COCO_CLASSES) == 80


def test_sim_returns_empty(sim_detector):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    result = sim_detector.detect(frame)
    assert result == []


def test_letterbox_output_shape(sim_detector):
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    lb = sim_detector._letterbox(frame)
    assert lb.shape == (640, 640, 3)


def test_letterbox_square_unchanged(sim_detector):
    frame = np.ones((640, 640, 3), dtype=np.uint8) * 128
    lb = sim_detector._letterbox(frame)
    assert lb.shape == (640, 640, 3)


def test_decode_empty_output(sim_detector):
    raw = np.zeros((80, 5, 100), dtype=np.float32)
    results = sim_detector._decode(raw, 640, 480)
    assert results == []


def test_decode_single_detection(sim_detector):
    raw = np.zeros((80, 5, 100), dtype=np.float32)
    # Person class (0), detection slot 0, score 0.9
    # bbox [y1, x1, y2, x2] normalized, centred at (0.5, 0.5), size 0.2
    raw[0, 0, 0] = 0.4   # y1
    raw[0, 1, 0] = 0.4   # x1
    raw[0, 2, 0] = 0.6   # y2
    raw[0, 3, 0] = 0.6   # x2
    raw[0, 4, 0] = 0.9   # score
    results = sim_detector._decode(raw, 640, 480)
    assert len(results) == 1
    d = results[0]
    assert d.label == "person"
    assert d.class_id == 0
    assert d.confidence == pytest.approx(0.9)
    # bbox should be pixel coords in 640×480 frame
    x1, y1, x2, y2 = d.bbox
    assert x1 < x2
    assert y1 < y2


def test_decode_below_threshold(sim_detector):
    raw = np.zeros((80, 5, 100), dtype=np.float32)
    raw[0, 0, 0] = 0.3   # y1
    raw[0, 1, 0] = 0.3   # x1
    raw[0, 2, 0] = 0.7   # y2
    raw[0, 3, 0] = 0.7   # x2
    raw[0, 4, 0] = 0.2   # score below threshold
    results = sim_detector._decode(raw, 640, 480)
    assert results == []


def test_filter_detections_removes_small_and_duplicate_boxes(sim_detector):
    dets = [
        Detection(label="person", class_id=0, confidence=0.9, bbox=[10.0, 10.0, 60.0, 60.0]),
        Detection(label="person", class_id=0, confidence=0.8, bbox=[15.0, 15.0, 65.0, 65.0]),
        Detection(label="bottle", class_id=39, confidence=0.6, bbox=[0.0, 0.0, 10.0, 10.0]),
    ]
    result = sim_detector.filter_detections(dets)
    assert len(result) == 1
    assert result[0].label == "person"


def test_filter_detections_respects_class_threshold(sim_detector):
    dets = [Detection(label="person", class_id=0, confidence=0.34, bbox=[0.0, 0.0, 100.0, 100.0])]
    result = sim_detector.filter_detections(dets)
    assert result == []


def test_detection_dataclass():
    d = Detection(label="cat", class_id=15, confidence=0.85, bbox=[10.0, 20.0, 100.0, 200.0])
    assert d.label == "cat"
    assert d.class_id == 15
    assert d.confidence == pytest.approx(0.85)
    assert len(d.bbox) == 4
