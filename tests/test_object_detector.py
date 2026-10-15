"""
Tests for ObjectDetector (sim mode — no Hailo device required).
"""
import numpy as np
import pytest

from src.perception.object_detector import (
    ObjectDetector, COCO_CLASSES, Detection, _ONNX_INPUT_NAMES_BY_VARIANT,
)


@pytest.fixture
def sim_detector():
    d = ObjectDetector.__new__(ObjectDetector)
    d._conf_threshold = 0.4
    d._min_box_area = 400
    d._iou_threshold = 0.55
    d._class_thresholds = {**{k: 0.25 for k in COCO_CLASSES}, "person": 0.35}
    d._engine = None
    d._onnx_session = None
    d._onnx_input_names = _ONNX_INPUT_NAMES_BY_VARIANT["yolo26n"]
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
    raw = np.zeros((300, 6), dtype=np.float32)  # all scores 0.0 -> filtered
    results = sim_detector._decode(raw, 640, 480)
    assert results == []


def test_decode_single_detection(sim_detector):
    raw = np.zeros((300, 6), dtype=np.float32)
    # Row format: [x1, y1, x2, y2, score, class_id] in 640x640 pixel space.
    # Person class (0), box centred in the 640x640 model space, high score.
    raw[0] = [200.0, 200.0, 400.0, 400.0, 0.9, 0.0]
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
    raw = np.zeros((300, 6), dtype=np.float32)
    raw[0] = [200.0, 200.0, 450.0, 450.0, 0.2, 0.0]  # score below threshold
    results = sim_detector._decode(raw, 640, 480)
    assert results == []


def test_decode_invalid_class_id_skipped(sim_detector):
    raw = np.zeros((300, 6), dtype=np.float32)
    raw[0] = [200.0, 200.0, 450.0, 450.0, 0.9, 999.0]  # out-of-range class id
    results = sim_detector._decode(raw, 640, 480)
    assert results == []


def test_decode_wrong_shape_returns_empty(sim_detector):
    raw = np.zeros((80, 5, 100), dtype=np.float32)  # old NMS-format shape, no longer supported
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


def test_sim_returns_empty_when_onnx_session_missing():
    d = ObjectDetector.__new__(ObjectDetector)
    d._sim = False
    d._engine = object()  # pretend hardware is ready
    d._onnx_session = None  # but ONNX sidecar missing
    result = d.detect(np.zeros((480, 640, 3), dtype=np.uint8))
    assert result == []


def test_run_onnx_postprocess_maps_hef_outputs_to_onnx_inputs(sim_detector):
    class _FakeOutput:
        def __init__(self, name):
            self.name = name

    class _FakeOnnxSession:
        def __init__(self):
            self._captured_inputs = None

        def get_outputs(self):
            return [_FakeOutput("output0")]

        def run(self, output_names, onnx_inputs):
            self._captured_inputs = onnx_inputs
            return [np.zeros((1, 300, 6), dtype=np.float32)]

    fake_session = _FakeOnnxSession()
    sim_detector._onnx_session = fake_session

    # Simulate HailoInference.infer() output: NHWC tensors keyed by full name.
    hef_outputs = {
        "yolo26n/conv61": np.zeros((80, 80, 4), dtype=np.float32),
        "yolo26n/conv77": np.zeros((40, 40, 4), dtype=np.float32),
        "yolo26n/conv91": np.zeros((20, 20, 4), dtype=np.float32),
        "yolo26n/conv64": np.zeros((80, 80, 80), dtype=np.float32),
        "yolo26n/conv80": np.zeros((40, 40, 80), dtype=np.float32),
        "yolo26n/conv94": np.zeros((20, 20, 80), dtype=np.float32),
    }
    out = sim_detector._run_onnx_postprocess(hef_outputs)
    assert out.shape == (300, 6)  # batch dim stripped

    # Verify NHWC -> NCHW transposition happened and all 6 onnx inputs were filled.
    assert fake_session._captured_inputs is not None
    assert len(fake_session._captured_inputs) == 6
    for onnx_name, tensor in fake_session._captured_inputs.items():
        assert tensor.shape[0] == 1  # batch dim present
        assert tensor.ndim == 4


def test_run_onnx_postprocess_missing_hef_output_raises(sim_detector):
    class _FakeOnnxSession:
        def get_outputs(self):
            return []

    sim_detector._onnx_session = _FakeOnnxSession()
    with pytest.raises(ValueError):
        sim_detector._run_onnx_postprocess({})


def test_detection_dataclass():
    d = Detection(label="cat", class_id=15, confidence=0.85, bbox=[10.0, 20.0, 100.0, 200.0])
    assert d.label == "cat"
    assert d.class_id == 15
    assert d.confidence == pytest.approx(0.85)
    assert len(d.bbox) == 4


def test_decode_nms_output(sim_detector):
    sim_detector._onchip_nms = True
    raw = np.zeros(40080, dtype=np.float32)

    # Packed HAILO_NMS_BY_CLASS layout (as emitted by HailoRT 5 on Hailo-10H):
    # per class: count K, then K × [y1, x1, y2, x2, score], no fixed stride.
    packed = [
        1.0, 0.2, 0.1, 0.8, 0.9, 0.92,   # class 0 person
        0.0,                             # class 1 bicycle: none
        1.0, 0.3, 0.4, 0.7, 0.8, 0.88,   # class 2 car
        0.0, 0.0,                        # classes 3-4: none
        1.0, 0.21, 0.02, 0.68, 0.99, 0.94,  # class 5 bus
    ]
    raw[: len(packed)] = packed

    outputs = {"yolov8m/yolov8_nms_postprocess": raw}
    detections = sim_detector._decode_nms(outputs, 640, 640)
    labels = {d.label: d.confidence for d in detections}
    assert labels.keys() == {"person", "car", "bus"}
    assert labels["person"] == pytest.approx(0.92)
    assert labels["car"] == pytest.approx(0.88)
    assert labels["bus"] == pytest.approx(0.94)



def test_decode_hailo8_vstreams_nms_list(sim_detector):
    """HailoRT 4.x VStreams returns 80 per-class (K, 5) arrays (verified on Hailo-8)."""
    sim_detector._onchip_nms = True
    per_class = [np.zeros((0, 5), np.float32) for _ in range(80)]
    per_class[0] = np.array([[0.369, 0.17, 0.837, 0.353, 0.93]], np.float32)
    per_class[5] = np.array([[0.21, 0.02, 0.68, 0.99, 0.95]], np.float32)
    detections = sim_detector._decode_nms({"yolov11l/yolov8_nms_postprocess": per_class}, 640, 640)
    labels = {d.label: d.confidence for d in detections}
    assert labels == {"bus": pytest.approx(0.95), "person": pytest.approx(0.93)}
    bus = next(d for d in detections if d.label == "bus")
    assert bus.bbox == pytest.approx([12.8, 134.4, 633.6, 435.2], abs=0.2)


def test_decode_nms_rejects_unknown_layout(sim_detector):
    assert sim_detector._decode_nms({"x/nms": np.zeros(10, np.float32)}, 640, 640) == []
