"""
Object detector — YOLO26m on Hailo-8.

Wraps the latest available YOLO26 HEF via :class:`~src.perception.hailo_inference.HailoInference`.
Input: any RGB frame (letterboxed to 640×640 internally).
Output: list of :class:`Detection` namedtuples.

HEF I/O signature
-----------------
  Input  ``yolo26m/input_layer1``        (640, 640, 3)  uint8 RGB
  Output ``yolo26m/yolo26_nms_postprocess`` (80, 5, 100) float32

Output layout: ``output[class_id, coord_idx, det_idx]``
  coord_idx 0–3 = [y1, x1, y2, x2] normalised 0–1 (Hailo NMS convention)
  coord_idx 4   = detection confidence score

Gracefully falls back to returning an empty list when the Hailo device
or HEF file is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# ── COCO-80 class labels ───────────────────────────────────────────────────
COCO_CLASSES: List[str] = [
    "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train",
    "truck", "boat", "traffic light", "fire hydrant", "stop sign",
    "parking meter", "bench", "bird", "cat", "dog", "horse", "sheep", "cow",
    "elephant", "bear", "zebra", "giraffe", "backpack", "umbrella", "handbag",
    "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball", "kite",
    "baseball bat", "baseball glove", "skateboard", "surfboard",
    "tennis racket", "bottle", "wine glass", "cup", "fork", "knife", "spoon",
    "bowl", "banana", "apple", "sandwich", "orange", "broccoli", "carrot",
    "hot dog", "pizza", "donut", "cake", "chair", "couch", "potted plant",
    "bed", "dining table", "toilet", "tv", "laptop", "mouse", "remote",
    "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
    "refrigerator", "book", "clock", "vase", "scissors", "teddy bear",
    "hair drier", "toothbrush",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEF_CANDIDATES = [
    _REPO_ROOT / "config" / "hailo" / "yolo26m.hef",
    _REPO_ROOT / "config" / "hailo" / "yolo26s.hef",
    _REPO_ROOT / "config" / "hailo" / "yolo26n.hef",
    Path("/usr/local/hailo/resources/models/hailo8/yolo26m.hef"),
    Path("/usr/local/hailo/resources/models/hailo8/yolo26s.hef"),
    Path("/usr/local/hailo/resources/models/hailo8/yolo26n.hef"),
]
_TARGET_SIZE = 640  # YOLO26 expects 640×640
_DEFAULT_CLASS_THRESHOLDS: Dict[str, float] = {
    "person": 0.35,
    "bicycle": 0.3,
    "car": 0.3,
    "motorcycle": 0.3,
    "bus": 0.3,
    "train": 0.3,
    "truck": 0.3,
    "boat": 0.3,
    "traffic light": 0.3,
    "fire hydrant": 0.3,
    "stop sign": 0.3,
    "parking meter": 0.3,
    "bench": 0.25,
    "bird": 0.25,
    "cat": 0.3,
    "dog": 0.3,
    "horse": 0.3,
    "sheep": 0.3,
    "cow": 0.3,
    "elephant": 0.3,
    "bear": 0.3,
    "zebra": 0.3,
    "giraffe": 0.3,
    "backpack": 0.25,
    "umbrella": 0.25,
    "handbag": 0.25,
    "tie": 0.25,
    "suitcase": 0.25,
    "frisbee": 0.25,
    "skis": 0.25,
    "snowboard": 0.25,
    "sports ball": 0.25,
    "kite": 0.25,
    "baseball bat": 0.25,
    "baseball glove": 0.25,
    "skateboard": 0.25,
    "surfboard": 0.25,
    "tennis racket": 0.25,
    "bottle": 0.25,
    "wine glass": 0.25,
    "cup": 0.25,
    "fork": 0.25,
    "knife": 0.25,
    "spoon": 0.25,
    "bowl": 0.25,
    "banana": 0.25,
    "apple": 0.25,
    "sandwich": 0.25,
    "orange": 0.25,
    "broccoli": 0.25,
    "carrot": 0.25,
    "hot dog": 0.25,
    "pizza": 0.25,
    "donut": 0.25,
    "cake": 0.25,
    "chair": 0.25,
    "couch": 0.25,
    "potted plant": 0.25,
    "bed": 0.25,
    "dining table": 0.25,
    "toilet": 0.25,
    "tv": 0.25,
    "laptop": 0.25,
    "mouse": 0.25,
    "remote": 0.25,
    "keyboard": 0.25,
    "cell phone": 0.25,
    "microwave": 0.2,
    "oven": 0.2,
    "toaster": 0.2,
    "sink": 0.2,
    "refrigerator": 0.2,
    "book": 0.2,
    "clock": 0.2,
    "vase": 0.2,
    "scissors": 0.2,
    "teddy bear": 0.2,
    "hair drier": 0.2,
    "toothbrush": 0.2,
}


@dataclass
class Detection:
    """A single detected object."""
    label: str
    class_id: int
    confidence: float
    bbox: List[float] = field(default_factory=list)  # [x1, y1, x2, y2] in pixels


class ObjectDetector:
    """
    YOLO26 object detector backed by Hailo-8.

    Parameters
    ----------
    conf_threshold : float
        Minimum confidence to report a detection (default 0.4).
    min_box_area : int
        Minimum bounding-box area in pixels before a detection is kept.
    class_thresholds : dict[str, float]
        Optional per-class confidence floor, used to reduce false positives.
    iou_threshold : float
        IoU threshold used to deduplicate overlapping detections in the same frame.
    """

    def __init__(
        self,
        conf_threshold: float = 0.4,
        min_box_area: int = 400,
        class_thresholds: Optional[Dict[str, float]] = None,
        iou_threshold: float = 0.55,
    ) -> None:
        self._conf_threshold = conf_threshold
        self._min_box_area = max(0, int(min_box_area))
        self._iou_threshold = float(iou_threshold)
        self._class_thresholds = {**_DEFAULT_CLASS_THRESHOLDS, **(class_thresholds or {})}
        self._engine = None
        self._sim = False
        self._backend = "sim"
        # Reusable model-input buffer; always 640×640×3 (model input size, not video resolution).
        self._letterbox_buf = np.zeros((_TARGET_SIZE, _TARGET_SIZE, 3), dtype=np.uint8)
        # Cached letterbox geometry — recomputed only when source frame dimensions change.
        self._lb_src_shape: tuple = (0, 0)
        self._lb_scale: float = 1.0
        self._lb_new_w: int = _TARGET_SIZE
        self._lb_new_h: int = _TARGET_SIZE
        self._lb_pad_top: int = 0
        self._lb_pad_left: int = 0
        self._init_engine()

    @staticmethod
    def _iou(box_a: List[float], box_b: List[float]) -> float:
        if len(box_a) != 4 or len(box_b) != 4:
            return 0.0
        x1 = max(box_a[0], box_b[0])
        y1 = max(box_a[1], box_b[1])
        x2 = min(box_a[2], box_b[2])
        y2 = min(box_a[3], box_b[3])
        inter_w = max(0.0, x2 - x1)
        inter_h = max(0.0, y2 - y1)
        inter = inter_w * inter_h
        area_a = max(0.0, (box_a[2] - box_a[0]) * (box_a[3] - box_a[1]))
        area_b = max(0.0, (box_b[2] - box_b[0]) * (box_b[3] - box_b[1]))
        union = area_a + area_b - inter
        if union <= 0.0:
            return 0.0
        return inter / union

    def filter_detections(self, detections: List[Detection]) -> List[Detection]:
        kept: List[Detection] = []
        for det in sorted(detections, key=lambda d: d.confidence, reverse=True):
            cls_threshold = self._class_thresholds.get(det.label, self._conf_threshold)
            if det.confidence < cls_threshold:
                continue
            width = max(0.0, det.bbox[2] - det.bbox[0])
            height = max(0.0, det.bbox[3] - det.bbox[1])
            area = width * height
            if self._min_box_area and area < self._min_box_area:
                continue
            if any(
                d.label == det.label and self._iou(d.bbox, det.bbox) > self._iou_threshold
                for d in kept
            ):
                continue
            kept.append(det)
        kept.sort(key=lambda d: d.confidence, reverse=True)
        return kept

    def _init_engine(self) -> None:
        try:
            from src.perception.hailo_inference import HailoInference
            hef_path = next((p for p in _HEF_CANDIDATES if p.exists()), None)
            if hef_path is None:
                raise FileNotFoundError("No YOLO26 HEF found")
            engine = HailoInference(hef_path)
            if engine.hardware_ready:
                self._engine = engine
                self._backend = "hailo"
                log.info("ObjectDetector: Hailo-8 backend ready (%s)", hef_path.name)
            else:
                log.warning("ObjectDetector: Hailo unavailable — sim mode (empty detections)")
                self._sim = True
        except Exception as exc:
            log.warning("ObjectDetector init failed (%s) — sim mode", exc)
            self._sim = True

    @property
    def backend(self) -> str:
        return self._backend

    def detect(self, frame: np.ndarray) -> List[Detection]:
        """
        Run object detection on *frame* (H×W×3 uint8 RGB).

        Returns a list of :class:`Detection` objects sorted by confidence
        descending. Returns an empty list in sim mode or on error.
        """
        if self._sim or self._engine is None:
            return []

        src_h, src_w = frame.shape[:2]
        try:
            inp = self._letterbox(frame)
            outputs = self._engine.infer({"input_layer1": inp})
        except Exception as exc:
            log.error("ObjectDetector.detect: inference failed — %s", exc)
            return []

        # Find the NMS output tensor (name may vary by HEF version)
        nms_key = next(
            (k for k in outputs if "nms" in k.lower() or "postprocess" in k.lower()),
            next(iter(outputs), None),
        )
        if nms_key is None or nms_key not in outputs:
            return []

        raw = outputs[nms_key]  # shape (80, 5, 100) after batch-dim strip
        return self._decode(raw, src_w, src_h)

    # ── Internal ───────────────────────────────────────────────────────

    def _letterbox(self, frame: np.ndarray) -> np.ndarray:
        """Resize *frame* (BGR) to 640×640 with letterboxing into the preallocated buffer.

        Geometry (scale, padding) is cached after the first call; ``buf.fill(0)``
        is only executed when frame dimensions change (e.g. resolution switch).
        The output is RGB because YOLOv8s expects RGB input.
        """
        import cv2
        src_h, src_w = frame.shape[:2]
        if (src_h, src_w) != self._lb_src_shape:
            scale = min(_TARGET_SIZE / src_h, _TARGET_SIZE / src_w)
            self._lb_new_w   = int(src_w * scale)
            self._lb_new_h   = int(src_h * scale)
            self._lb_scale   = scale
            self._lb_pad_top  = (_TARGET_SIZE - self._lb_new_h) // 2
            self._lb_pad_left = (_TARGET_SIZE - self._lb_new_w) // 2
            self._lb_src_shape = (src_h, src_w)
            self._letterbox_buf.fill(0)  # re-zero only on geometry change
        resized = cv2.resize(frame, (self._lb_new_w, self._lb_new_h),
                             interpolation=cv2.INTER_LINEAR)
        pt, pl = self._lb_pad_top, self._lb_pad_left
        # Copy as RGB (model expects RGB; camera delivers BGR)
        self._letterbox_buf[pt:pt + self._lb_new_h, pl:pl + self._lb_new_w] = resized[:, :, ::-1]
        return self._letterbox_buf

    def _decode(
        self,
        raw,
        src_w: int,
        src_h: int,
    ) -> List[Detection]:
        """Decode NMS output into :class:`Detection` objects.

        Hailo's NMS postprocess output at runtime is a **list** of 80 per-class
        arrays, each shaped ``(N, 5)`` where N is the number of detections for
        that class and the 5 columns are ``[y1, x1, y2, x2, score]`` in
        normalised 0–1 coordinates (Hailo convention, y before x).

        The HEF metadata reports ``(80, 5, 100)`` as the max-capacity shape, but
        the actual runtime value is the variable-length list format.
        """
        # Reuse cached letterbox params when possible (avoids duplicate float math).
        if (src_h, src_w) == self._lb_src_shape:
            scale    = self._lb_scale
            pad_top  = self._lb_pad_top
            pad_left = self._lb_pad_left
        else:
            scale    = min(_TARGET_SIZE / src_h, _TARGET_SIZE / src_w)
            new_w    = int(src_w * scale)
            new_h    = int(src_h * scale)
            pad_top  = (_TARGET_SIZE - new_h) // 2
            pad_left = (_TARGET_SIZE - new_w) // 2

        detections: List[Detection] = []

        # ── List format (runtime NMS output) ──────────────────────────
        if isinstance(raw, (list, tuple)):
            for cls_id, cls_dets in enumerate(raw):
                if cls_id >= len(COCO_CLASSES):
                    break
                if cls_dets is None or len(cls_dets) == 0:
                    continue
                arr = np.asarray(cls_dets, dtype=np.float32)
                if arr.ndim == 1:
                    arr = arr[np.newaxis]  # single detection → (1, 5)
                for det in arr:
                    if len(det) < 5:
                        continue
                    score = float(det[4])
                    if score < self._conf_threshold:
                        continue
                    y1n, x1n, y2n, x2n = float(det[0]), float(det[1]), float(det[2]), float(det[3])
                    x1 = max(0.0, (x1n * _TARGET_SIZE - pad_left) / scale)
                    y1 = max(0.0, (y1n * _TARGET_SIZE - pad_top)  / scale)
                    x2 = min(float(src_w), (x2n * _TARGET_SIZE - pad_left) / scale)
                    y2 = min(float(src_h), (y2n * _TARGET_SIZE - pad_top)  / scale)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    detections.append(Detection(
                        label=COCO_CLASSES[cls_id],
                        class_id=cls_id,
                        confidence=round(score, 3),
                        bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    ))

        # ── Fixed tensor format (80, 5, 100) — fallback ───────────────
        elif isinstance(raw, np.ndarray) and raw.ndim == 3:
            num_classes, _, max_det = raw.shape
            for cls_id in range(min(num_classes, len(COCO_CLASSES))):
                for det_id in range(max_det):
                    score = float(raw[cls_id, 4, det_id])
                    if score < self._conf_threshold:
                        continue
                    y1n = float(raw[cls_id, 0, det_id])
                    x1n = float(raw[cls_id, 1, det_id])
                    y2n = float(raw[cls_id, 2, det_id])
                    x2n = float(raw[cls_id, 3, det_id])
                    x1 = max(0.0, (x1n * _TARGET_SIZE - pad_left) / scale)
                    y1 = max(0.0, (y1n * _TARGET_SIZE - pad_top)  / scale)
                    x2 = min(float(src_w), (x2n * _TARGET_SIZE - pad_left) / scale)
                    y2 = min(float(src_h), (y2n * _TARGET_SIZE - pad_top)  / scale)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    detections.append(Detection(
                        label=COCO_CLASSES[cls_id],
                        class_id=cls_id,
                        confidence=round(score, 3),
                        bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
                    ))

        else:
            log.warning("ObjectDetector: unrecognised output type %s — skipping", type(raw))

        detections.sort(key=lambda d: d.confidence, reverse=True)
        return self.filter_detections(detections)

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None
