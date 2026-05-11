"""
SCRFD face detector — Hailo-8 primary, OpenCV Haar cascade fallback.

Architecture
------------
* **Hailo path**: Loads ``scrfd_10g.hef`` via HailoInference, runs the
  640×640 SCRFD-10G model, decodes multi-scale outputs into bounding
  boxes and (optionally) facial landmarks, applies NMS.
* **CPU path**: Falls back to OpenCV's built-in Haar cascade
  (``haarcascade_frontalface_default.xml``) — lower accuracy but zero
  extra dependencies.
* **Sim path**: Returns empty list, logs the call. Used in unit tests
  and dev machines without hardware.

SCRFD-10G output layout (3 scales, 2 anchors per cell)
-------------------------------------------------------
For each scale s ∈ {8, 16, 32} (feature-map sizes 80×80, 40×40, 20×20
for a 640×640 input):

  score  : (H, W,  2)   — per-anchor confidence (sigmoid)
  bbox   : (H, W,  8)   — 2 anchors × 4 distances [l, t, r, b]
  kps    : (H, W, 20)   — 2 anchors × 5 landmarks × 2 (x, y)

Bounding box decode:
  cx = (col + 0.5) * stride
  cy = (row + 0.5) * stride
  x1 = cx - l * stride;  y1 = cy - t * stride
  x2 = cx + r * stride;  y2 = cy + b * stride
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

from src.perception.hailo_inference import HailoInference

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_HEF = _REPO_ROOT / "config" / "hailo" / "scrfd_10g.hef"
_HAILO_SYSTEM_HEF = Path("/usr/local/hailo/resources/models/hailo8/scrfd_10g.hef")

# SCRFD-10G constants
_INPUT_SIZE = 640          # model expects 640×640 RGB uint8
_STRIDES = [8, 16, 32]     # feature-map strides → sizes 80×80, 40×40, 20×20
_NUM_ANCHORS = 2           # anchors per spatial cell at each scale


@dataclass
class FaceDetection:
    """One detected face."""
    bbox: Tuple[int, int, int, int]          # x1, y1, x2, y2 in original image pixels
    confidence: float
    centroid: Tuple[int, int]                # (cx, cy) centre of bbox
    landmarks: Optional[List[Tuple[int, int]]] = None  # 5 × (x, y), optional


class FaceDetector:
    """
    Detect faces in a BGR/RGB frame.

    Parameters
    ----------
    conf_threshold : float
        Minimum face confidence (0–1). Default 0.45.
    nms_threshold : float
        IoU threshold for non-maximum suppression. Default 0.4.
    hef_path : str | Path | None
        Explicit path to scrfd_10g.hef. When None the detector tries
        ``config/hailo/scrfd_10g.hef`` then the system install path.
    """

    def __init__(
        self,
        conf_threshold: float = 0.45,
        nms_threshold: float = 0.4,
        hef_path: Optional[str | Path] = None,
    ) -> None:
        self._conf_thr = conf_threshold
        self._nms_thr = nms_threshold
        self._engine: Optional[HailoInference] = None
        self._haar: Optional[cv2.CascadeClassifier] = None
        self._backend = self._init_backend(hef_path)

    # ── Backend selection ──────────────────────────────────────────────

    def _init_backend(self, hef_override) -> str:
        hef = self._find_hef(hef_override)
        if hef is not None:
            engine = HailoInference(hef)
            if engine.hardware_ready:
                self._engine = engine
                log.info("FaceDetector: Hailo-8 backend (%s)", Path(hef).name)
                return "hailo"
            engine.close()

        # CPU fallback — OpenCV Haar
        _HAAR_SEARCH = [
            "/usr/share/opencv4/haarcascades/haarcascade_frontalface_default.xml",
            "/usr/share/opencv/haarcascades/haarcascade_frontalface_default.xml",
        ]
        cascade_path = next((p for p in _HAAR_SEARCH if Path(p).exists()), None)
        if cascade_path is None:
            try:
                import cv2.data as _cvdata
                cascade_path = _cvdata.haarcascades + "haarcascade_frontalface_default.xml"
            except (ImportError, AttributeError):
                cascade_path = None
        if cascade_path:
            cascade = cv2.CascadeClassifier(cascade_path)
            if not cascade.empty():
                self._haar = cascade
                log.warning("FaceDetector: CPU Haar fallback (lower accuracy)")
                return "cpu"

        log.warning("[sim] FaceDetector: no backend available — sim mode")
        return "sim"

    @staticmethod
    def _find_hef(override) -> Optional[Path]:
        if override is not None:
            p = Path(override)
            return p if p.exists() else None
        for candidate in (_DEFAULT_HEF, _HAILO_SYSTEM_HEF):
            if candidate.exists():
                return candidate
        return None

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return self._backend == "hailo"

    @property
    def backend(self) -> str:
        return self._backend

    # ── Public API ─────────────────────────────────────────────────────

    def detect(self, frame: np.ndarray) -> List[FaceDetection]:
        """
        Detect faces in *frame*.

        Parameters
        ----------
        frame : np.ndarray
            H×W×3 uint8 image (RGB or BGR — both work; colour accuracy
            doesn't affect detection significantly for SCRFD/Haar).

        Returns
        -------
        list[FaceDetection]
            Detections sorted by confidence descending.
            Bounding-box coordinates are in original frame pixel space.
        """
        if self._backend == "hailo":
            return self._detect_hailo(frame)
        if self._backend == "cpu":
            return self._detect_haar(frame)
        log.debug("[sim] FaceDetector.detect() — returning empty list")
        return []

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close()
            self._engine = None

    def __enter__(self) -> "FaceDetector":
        return self

    def __exit__(self, *_) -> None:
        self.close()

    # ── Hailo path ─────────────────────────────────────────────────────

    def _detect_hailo(self, frame: np.ndarray) -> List[FaceDetection]:
        h_orig, w_orig = frame.shape[:2]
        blob, scale_x, scale_y, pad_x, pad_y = self._preprocess(frame)

        outputs = self._engine.infer({"input_layer1": blob})
        if not outputs:
            return []

        boxes, scores, kps_list = self._decode_scrfd(outputs, _INPUT_SIZE)

        if len(boxes) == 0:
            return []

        # NMS
        keep = cv2.dnn.NMSBoxes(
            [list(map(float, b)) for b in boxes],
            [float(s) for s in scores],
            self._conf_thr,
            self._nms_thr,
        )
        if len(keep) == 0:
            return []

        results: List[FaceDetection] = []
        for idx in (keep.flatten() if hasattr(keep, "flatten") else keep):
            x1, y1, x2, y2 = boxes[idx]
            # Map from padded 640×640 space back to original image
            x1 = int(max(0, (x1 - pad_x) / scale_x))
            y1 = int(max(0, (y1 - pad_y) / scale_y))
            x2 = int(min(w_orig, (x2 - pad_x) / scale_x))
            y2 = int(min(h_orig, (y2 - pad_y) / scale_y))
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            lm = None
            if kps_list is not None:
                pts = kps_list[idx]
                lm = [
                    (
                        int((pts[i * 2] - pad_x) / scale_x),
                        int((pts[i * 2 + 1] - pad_y) / scale_y),
                    )
                    for i in range(5)
                ]
            results.append(
                FaceDetection(
                    bbox=(x1, y1, x2, y2),
                    confidence=float(scores[idx]),
                    centroid=(cx, cy),
                    landmarks=lm,
                )
            )
        results.sort(key=lambda d: d.confidence, reverse=True)
        return results

    # ── SCRFD preprocessing ────────────────────────────────────────────

    @staticmethod
    def _preprocess(
        frame: np.ndarray,
    ) -> tuple[np.ndarray, float, float, float, float]:
        """
        Letterbox-resize frame to 640×640 uint8.

        Returns
        -------
        blob         : np.ndarray  (640, 640, 3) uint8
        scale_x      : float  width scale factor
        scale_y      : float  height scale factor
        pad_x        : float  horizontal padding (pixels)
        pad_y        : float  vertical padding (pixels)
        """
        h, w = frame.shape[:2]
        scale = min(_INPUT_SIZE / w, _INPUT_SIZE / h)
        new_w, new_h = int(w * scale), int(h * scale)
        resized = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
        blob = np.zeros((_INPUT_SIZE, _INPUT_SIZE, 3), dtype=np.uint8)
        pad_x = (_INPUT_SIZE - new_w) // 2
        pad_y = (_INPUT_SIZE - new_h) // 2
        blob[pad_y : pad_y + new_h, pad_x : pad_x + new_w] = resized
        return blob, scale, scale, pad_x, pad_y

    # ── SCRFD output decode ────────────────────────────────────────────

    def _decode_scrfd(
        self,
        outputs: dict[str, np.ndarray],
        input_size: int,
    ) -> tuple[list, list, Optional[list]]:
        """
        Decode raw SCRFD outputs into flat lists of boxes, scores, landmarks.

        The model has 9 output tensors (3 scales × 3 types):
          score : (H, W,  2)
          bbox  : (H, W,  8)   [l, t, r, b] × 2 anchors
          kps   : (H, W, 20)   [x,y] × 5 × 2 anchors

        Output tensor names follow the pattern used by scrfd_10g.hef on Hailo
        (e.g. ``scrfd_10g/conv41``, ``scrfd_10g/conv42``, ``scrfd_10g/conv43``).
        We group them by feature-map spatial size to avoid hardcoding names.
        """
        # Group outputs by spatial size (80, 40, 20)
        by_size: dict[int, dict[str, np.ndarray]] = {}
        for name, arr in outputs.items():
            if arr.ndim < 2:
                continue
            size = arr.shape[0]  # H (==W for SCRFD)
            by_size.setdefault(size, {})[name] = arr

        all_boxes: list = []
        all_scores: list = []
        all_kps: list = []
        has_kps = False

        for stride in _STRIDES:
            fm_size = input_size // stride
            tensors = by_size.get(fm_size, {})
            if not tensors:
                continue

            # Sort tensors in this group by last dim: 2=score, 8=bbox, 20=kps
            by_ch: dict[int, np.ndarray] = {v.shape[-1]: v for v in tensors.values()}
            score_map = by_ch.get(2)
            bbox_map = by_ch.get(8)
            kps_map = by_ch.get(20)

            if score_map is None or bbox_map is None:
                continue

            H, W = fm_size, fm_size
            # Anchor centres: (H, W, 2, 2) → centre (cy, cx) for each cell
            cols = np.arange(W, dtype=np.float32)
            rows = np.arange(H, dtype=np.float32)
            grid_x, grid_y = np.meshgrid(cols, rows)
            # centre in model-input pixel space
            cx = (grid_x + 0.5) * stride  # (H, W)
            cy = (grid_y + 0.5) * stride

            # score_map: (H, W, 2) → sigmoid
            scores = 1.0 / (1.0 + np.exp(-score_map.astype(np.float32)))  # (H, W, 2)

            # bbox_map: (H, W, 8) → (H, W, 2, 4)
            bbox = bbox_map.astype(np.float32).reshape(H, W, _NUM_ANCHORS, 4)

            if kps_map is not None:
                kps = kps_map.astype(np.float32).reshape(H, W, _NUM_ANCHORS, 10)
                has_kps = True
            else:
                kps = None

            for a in range(_NUM_ANCHORS):
                score_a = scores[:, :, a]  # (H, W)
                mask = score_a > self._conf_thr
                if not np.any(mask):
                    continue

                ys, xs = np.where(mask)
                s = score_a[ys, xs]

                l = bbox[ys, xs, a, 0] * stride
                t = bbox[ys, xs, a, 1] * stride
                r = bbox[ys, xs, a, 2] * stride
                b = bbox[ys, xs, a, 3] * stride

                x1 = cx[ys, xs] - l
                y1 = cy[ys, xs] - t
                x2 = cx[ys, xs] + r
                y2 = cy[ys, xs] + b

                for i in range(len(ys)):
                    all_boxes.append((
                        float(x1[i]), float(y1[i]),
                        float(x2[i] - x1[i]),   # cv2.dnn.NMSBoxes wants [x,y,w,h]
                        float(y2[i] - y1[i]),
                    ))
                    all_scores.append(float(s[i]))
                    if kps is not None:
                        kp = kps[ys[i], xs[i], a]  # (10,)
                        pts = kp.copy()
                        pts[0::2] = pts[0::2] * stride + cx[ys[i], xs[i]]
                        pts[1::2] = pts[1::2] * stride + cy[ys[i], xs[i]]
                        all_kps.append(pts)
                    else:
                        all_kps.append(None)

        # Convert xywh boxes back to x1y1x2y2 after NMS pass
        # (NMSBoxes returns indices into all_boxes which are xywh)
        boxes_x1y1x2y2 = [
            (b[0], b[1], b[0] + b[2], b[1] + b[3]) for b in all_boxes
        ]

        return boxes_x1y1x2y2, all_scores, all_kps if has_kps else None

    # ── CPU Haar path ──────────────────────────────────────────────────

    def _detect_haar(self, frame: np.ndarray) -> List[FaceDetection]:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        rects = self._haar.detectMultiScale(
            gray,
            scaleFactor=1.1,
            minNeighbors=5,
            minSize=(30, 30),
        )
        results: List[FaceDetection] = []
        for x, y, w, h in (rects if len(rects) > 0 else []):
            results.append(
                FaceDetection(
                    bbox=(int(x), int(y), int(x + w), int(y + h)),
                    confidence=1.0,
                    centroid=(int(x + w // 2), int(y + h // 2)),
                )
            )
        return results
