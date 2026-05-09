"""
Object detector — YOLOv8s on Hailo-8.

Wraps the ``yolov8s.hef`` model via :class:`~src.perception.hailo_inference.HailoInference`.
Input: any RGB frame (letterboxed to 640×640 internally).
Output: list of :class:`Detection` namedtuples.

HEF I/O signature
-----------------
  Input  ``yolov8s/input_layer1``        (640, 640, 3)  uint8 RGB
  Output ``yolov8s/yolov8_nms_postprocess`` (80, 5, 100) float32

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
from typing import List, Optional

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

_HEF_PATH = Path("/usr/local/hailo/resources/models/hailo8/yolov8s.hef")
_TARGET_SIZE = 640  # YOLOv8s expects 640×640


@dataclass
class Detection:
    """A single detected object."""
    label: str
    class_id: int
    confidence: float
    bbox: List[float] = field(default_factory=list)  # [x1, y1, x2, y2] in pixels


class ObjectDetector:
    """
    YOLOv8s object detector backed by Hailo-8.

    Parameters
    ----------
    conf_threshold : float
        Minimum confidence to report a detection (default 0.4).
    """

    def __init__(self, conf_threshold: float = 0.4) -> None:
        self._conf_threshold = conf_threshold
        self._engine = None
        self._sim = False
        self._backend = "sim"
        self._init_engine()

    def _init_engine(self) -> None:
        try:
            from src.perception.hailo_inference import HailoInference
            engine = HailoInference(_HEF_PATH)
            if engine.hardware_ready:
                self._engine = engine
                self._backend = "hailo"
                log.info("ObjectDetector: Hailo-8 backend ready (yolov8s)")
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
        """Resize *frame* to 640×640 with letterboxing (black padding)."""
        import cv2
        src_h, src_w = frame.shape[:2]
        scale = min(_TARGET_SIZE / src_h, _TARGET_SIZE / src_w)
        new_w = int(src_w * scale)
        new_h = int(src_h * scale)
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        canvas = np.zeros((_TARGET_SIZE, _TARGET_SIZE, 3), dtype=np.uint8)
        pad_top  = (_TARGET_SIZE - new_h) // 2
        pad_left = (_TARGET_SIZE - new_w) // 2
        canvas[pad_top:pad_top + new_h, pad_left:pad_left + new_w] = resized
        return canvas

    def _decode(
        self,
        raw: np.ndarray,
        src_w: int,
        src_h: int,
    ) -> List[Detection]:
        """Decode NMS output tensor into :class:`Detection` objects."""
        if raw.ndim != 3 or raw.shape[0] != 80 or raw.shape[1] < 5:
            log.warning("ObjectDetector: unexpected output shape %s — skipping", raw.shape)
            return []

        # Letterbox params (must mirror _letterbox exactly)
        scale    = min(_TARGET_SIZE / src_h, _TARGET_SIZE / src_w)
        new_w    = int(src_w * scale)
        new_h    = int(src_h * scale)
        pad_top  = (_TARGET_SIZE - new_h) // 2
        pad_left = (_TARGET_SIZE - new_w) // 2

        detections: List[Detection] = []
        num_classes, _, max_det = raw.shape

        for cls_id in range(min(num_classes, len(COCO_CLASSES))):
            for det_id in range(max_det):
                score = float(raw[cls_id, 4, det_id])
                if score < self._conf_threshold:
                    continue

                # Hailo NMS convention: [y1, x1, y2, x2] normalised 0–1
                y1n = float(raw[cls_id, 0, det_id])
                x1n = float(raw[cls_id, 1, det_id])
                y2n = float(raw[cls_id, 2, det_id])
                x2n = float(raw[cls_id, 3, det_id])

                # Un-letterbox → pixel coordinates in the original frame
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

        detections.sort(key=lambda d: d.confidence, reverse=True)
        return detections

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None
