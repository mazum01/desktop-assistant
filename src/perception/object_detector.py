"""
Object detector — YOLO26m on Hailo-8 (split HEF + ONNX postprocessing).

YOLO26 (Ultralytics, 2026) is NMS-free by design: the HEF only runs the
backbone/detection-head "neural processing" and outputs raw per-scale
regression + classification conv tensors. There is no on-device NMS
output for YOLO26 the way there was for YOLOv8/v11 — the final box
assignment/decoding step is done by a small companion ONNX model run on
the host CPU via onnxruntime. This module wires both stages together:

  1. HailoInference runs ``yolo26m.hef`` and returns 6 raw conv tensors.
  2. Those tensors are fed into ``yolo26n_postprocessing.onnx`` (a lightweight
     ONNX Runtime session — this decode op has no learned weights tied to
     model size, so the same sidecar works for any YOLO26 HEF variant)
     which reproduces the final decode step.
  3. The ONNX output (300, 6) = [x1, y1, x2, y2, score, class_id] in
     640x640 pixel space is converted into :class:`Detection` objects.

HEF I/O signature (yolo26m — conv layer numbers only, shapes are identical
across YOLO26 sizes since they're purely a function of the 640x640 input
and 80-class COCO head):
---------------------------------------------------------------------
  Input  ``yolo26m/input_layer1``  (640, 640, 3) uint8 RGB
  Output ``yolo26m/conv71``  (80, 80, 4)   regression, scale 1/8
  Output ``yolo26m/conv87``  (40, 40, 4)   regression, scale 1/16
  Output ``yolo26m/conv101`` (20, 20, 4)   regression, scale 1/32
  Output ``yolo26m/conv74``  (80, 80, 80)  classification, scale 1/8
  Output ``yolo26m/conv90``  (40, 40, 80)  classification, scale 1/16
  Output ``yolo26m/conv104`` (20, 20, 80)  classification, scale 1/32

Hailo's Model Zoo (v2.19+) publishes yolo26n/s/m HEFs for Hailo-8 (see
https://github.com/hailo-ai/hailo_model_zoo — HAILO8 object detection
table). We use yolo26m over yolo26n for materially better accuracy
(~52.3 vs ~40.0 mAP on COCO) at the cost of throughput (~95 vs ~427 FPS
on Hailo-8) — acceptable since this assistant only needs a few detections
per second, not video-rate throughput. yolo26n remains available as a
fallback candidate. Larger l/x sizes are not published for Hailo-8.

Gracefully falls back to returning an empty list when the Hailo device,
HEF, or ONNX sidecar is unavailable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

log = logging.getLogger(__name__)

# Maps HEF conv output name -> ONNX postprocessing model input name. The conv
# layer *numbers* differ per YOLO26 size (n/s/m each get compiled with a
# different internal layer count) but the postprocessing ONNX sidecar itself
# is size-agnostic (pure box-decode/top-k math, no learned weights) — same
# yolo26n_postprocessing.onnx file works for every size, only this name map
# changes.
_ONNX_INPUT_NAMES_BY_VARIANT: Dict[str, Dict[str, str]] = {
    "yolo26n": {
        "conv61": "/model.23/one2one_cv2.0/one2one_cv2.0.2/Conv_output_0",
        "conv77": "/model.23/one2one_cv2.1/one2one_cv2.1.2/Conv_output_0",
        "conv91": "/model.23/one2one_cv2.2/one2one_cv2.2.2/Conv_output_0",
        "conv64": "/model.23/one2one_cv3.0/one2one_cv3.0.2/Conv_output_0",
        "conv80": "/model.23/one2one_cv3.1/one2one_cv3.1.2/Conv_output_0",
        "conv94": "/model.23/one2one_cv3.2/one2one_cv3.2.2/Conv_output_0",
    },
    "yolo26m": {
        "conv71":  "/model.23/one2one_cv2.0/one2one_cv2.0.2/Conv_output_0",
        "conv87":  "/model.23/one2one_cv2.1/one2one_cv2.1.2/Conv_output_0",
        "conv101": "/model.23/one2one_cv2.2/one2one_cv2.2.2/Conv_output_0",
        "conv74":  "/model.23/one2one_cv3.0/one2one_cv3.0.2/Conv_output_0",
        "conv90":  "/model.23/one2one_cv3.1/one2one_cv3.1.2/Conv_output_0",
        "conv104": "/model.23/one2one_cv3.2/one2one_cv3.2.2/Conv_output_0",
    },
}
_ONNX_REG_CHANNELS = 4  # regression heads have 4 channels (box params)

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
# YOLO26m gives materially better accuracy than yolo26n (~52.3 vs ~40.0 mAP)
# at acceptable throughput cost for this assistant's needs; yolo26n.hef is
# kept as a fallback candidate if yolo26m is ever unavailable. Both variants
# use the same yolo26n_postprocessing.onnx sidecar (see _ONNX_INPUT_NAMES_BY_VARIANT).
_HEF_CANDIDATES = [
    (_REPO_ROOT / "config" / "hailo" / "yolo26m.hef", "yolo26m"),
    (Path("/usr/local/hailo/resources/models/hailo8/yolo26m.hef"), "yolo26m"),
    (_REPO_ROOT / "config" / "hailo" / "yolo26n.hef", "yolo26n"),
    (Path("/usr/local/hailo/resources/models/hailo8/yolo26n.hef"), "yolo26n"),
]
_ONNX_SIDECAR_CANDIDATES = [
    _REPO_ROOT / "config" / "hailo" / "yolo26n_postprocessing.onnx",
    Path("/usr/local/hailo/resources/models/hailo8/yolo26n_postprocessing.onnx"),
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
        self._onnx_session = None
        self._onnx_input_names: Dict[str, str] = {}
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
            match = next(((p, variant) for p, variant in _HEF_CANDIDATES if p.exists()), None)
            if match is None:
                raise FileNotFoundError("No YOLO26 HEF found (yolo26m or yolo26n)")
            hef_path, variant = match
            onnx_path = next((p for p in _ONNX_SIDECAR_CANDIDATES if p.exists()), None)
            if onnx_path is None:
                raise FileNotFoundError(
                    "No YOLO26 ONNX postprocessing sidecar found — YOLO26 is "
                    "NMS-free and requires this companion model to decode boxes"
                )

            import onnxruntime as ort
            onnx_session = ort.InferenceSession(str(onnx_path))

            engine = HailoInference(hef_path)
            if engine.hardware_ready:
                self._engine = engine
                self._onnx_session = onnx_session
                self._onnx_input_names = _ONNX_INPUT_NAMES_BY_VARIANT[variant]
                self._backend = "hailo"
                log.info(
                    "ObjectDetector: Hailo-8 backend ready (%s [%s] + %s)",
                    hef_path.name, variant, onnx_path.name,
                )
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
        if self._sim or self._engine is None or self._onnx_session is None:
            return []

        src_h, src_w = frame.shape[:2]
        try:
            inp = self._letterbox(frame)
            hef_outputs = self._engine.infer({"input_layer1": inp})
            onnx_out = self._run_onnx_postprocess(hef_outputs)
        except Exception as exc:
            log.error("ObjectDetector.detect: inference failed — %s", exc)
            return []

        return self._decode(onnx_out, src_w, src_h)

    def _run_onnx_postprocess(self, hef_outputs: Dict[str, np.ndarray]) -> np.ndarray:
        """
        Map raw HEF conv-tensor outputs to the ONNX postprocessing model's
        inputs, run it, and return the ``(300, 6)`` detection tensor
        ``[x1, y1, x2, y2, score, class_id]`` in 640×640 pixel space.
        """
        onnx_inputs: Dict[str, np.ndarray] = {}
        for conv_name, onnx_name in self._onnx_input_names.items():
            # HEF output dict keys are prefixed with the network name, e.g.
            # "yolo26m/conv71" — match by suffix so this works regardless of prefix.
            hef_key = next(
                (k for k in hef_outputs if k.split("/")[-1] == conv_name), None
            )
            if hef_key is None:
                raise ValueError(f"Expected HEF output for '{conv_name}' not found "
                                  f"(available: {list(hef_outputs.keys())})")
            tensor = np.asarray(hef_outputs[hef_key], dtype=np.float32)
            # Hailo delivers NHWC; the ONNX model expects NCHW.
            if tensor.ndim == 3:  # (H, W, C) -> (1, C, H, W)
                tensor = np.transpose(tensor, (2, 0, 1))[np.newaxis]
            elif tensor.ndim == 4 and tensor.shape[-1] in (_ONNX_REG_CHANNELS, len(COCO_CLASSES)):
                tensor = np.transpose(tensor, (0, 3, 1, 2))  # NHWC -> NCHW
            onnx_inputs[onnx_name] = tensor

        output_names = [o.name for o in self._onnx_session.get_outputs()]
        results = self._onnx_session.run(output_names, onnx_inputs)
        out = np.asarray(results[0], dtype=np.float32)
        if out.ndim == 3:  # strip batch dim
            out = out[0]
        return out

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
        raw: np.ndarray,
        src_w: int,
        src_h: int,
    ) -> List[Detection]:
        """Decode the YOLO26 ONNX postprocessing output into :class:`Detection` objects.

        *raw* is the ``(300, 6)`` tensor produced by the postprocessing ONNX
        model: each row is ``[x1, y1, x2, y2, score, class_id]`` in 640×640
        pixel space (YOLO26 is NMS-free — this is already the final,
        de-duplicated detection set; no on-device NMS output exists).
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

        if not isinstance(raw, np.ndarray) or raw.ndim != 2 or raw.shape[-1] < 6:
            log.warning("ObjectDetector: unrecognised ONNX output shape %s — skipping",
                        getattr(raw, "shape", type(raw)))
            return []

        for row in raw:
            score = float(row[4])
            if score < self._conf_threshold:
                continue
            cls_id = int(row[5])
            if cls_id < 0 or cls_id >= len(COCO_CLASSES):
                continue
            x1p, y1p, x2p, y2p = float(row[0]), float(row[1]), float(row[2]), float(row[3])
            x1 = max(0.0, (x1p - pad_left) / scale)
            y1 = max(0.0, (y1p - pad_top) / scale)
            x2 = min(float(src_w), (x2p - pad_left) / scale)
            y2 = min(float(src_h), (y2p - pad_top) / scale)
            if x2 <= x1 or y2 <= y1:
                continue
            detections.append(Detection(
                label=COCO_CLASSES[cls_id],
                class_id=cls_id,
                confidence=round(score, 3),
                bbox=[round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1)],
            ))

        detections.sort(key=lambda d: d.confidence, reverse=True)
        return self.filter_detections(detections)

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None
        self._onnx_session = None
