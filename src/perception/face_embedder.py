"""
Face embedding extractor — ArcFace MobileFaceNet on Hailo-8.

Takes a raw camera frame and a detected face's 5 landmark points (left eye,
right eye, nose, left mouth, right mouth — SCRFD order), aligns the face to
the 112×112 ArcFace canonical pose, and returns a 512-dim L2-normalised
embedding vector.

Usage::

    embedder = FaceEmbedder()
    embedding = embedder.embed(frame, landmarks)   # np.ndarray shape (512,)

In sim mode (no Hailo device) ``embed()`` returns a zero vector so callers
can always treat the return value uniformly.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

import cv2
import numpy as np

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_HEF = Path("/usr/local/hailo/resources/models/hailo8/arcface_mobilefacenet.hef")
_FALLBACK_HEF = _REPO_ROOT / "config" / "hailo" / "arcface_mobilefacenet.hef"

# Reference 5-point landmarks in the 112×112 ArcFace canonical space.
# Order matches SCRFD output: left_eye, right_eye, nose, left_mouth, right_mouth.
_ARCFACE_REF = np.array(
    [
        [38.2946, 51.6963],  # left eye
        [73.5318, 51.5014],  # right eye
        [56.0252, 71.7366],  # nose
        [41.5493, 92.3655],  # left mouth
        [70.7299, 92.2041],  # right mouth
    ],
    dtype=np.float32,
)

_EMBED_DIM = 512


class FaceEmbedder:
    """Extract ArcFace embeddings via the Hailo-8 MobileFaceNet HEF.

    Sim mode is entered automatically when the Hailo device is unavailable;
    ``embed()`` returns a zero vector in that case.
    """

    def __init__(self, hef_path: Optional[str | Path] = None) -> None:
        hef = Path(hef_path) if hef_path else (
            _DEFAULT_HEF if _DEFAULT_HEF.exists() else _FALLBACK_HEF
        )
        self._sim = False
        self._engine = None
        self._input_name: str = "input_layer1"
        self._output_name: str = "output_layer1"

        try:
            from src.perception.hailo_inference import HailoInference
            engine = HailoInference(hef)
            if not engine.hardware_ready:
                log.info("[sim] FaceEmbedder — Hailo unavailable, using sim mode")
                self._sim = True
                engine.close()
            else:
                self._engine = engine
                # Discover actual I/O tensor names from the loaded model
                if engine.input_info:
                    self._input_name = engine.input_info[0].name
                if engine.output_info:
                    self._output_name = engine.output_info[0].name
                log.info(
                    "FaceEmbedder ready: input=%s  output=%s",
                    self._input_name, self._output_name,
                )
        except Exception as exc:
            log.warning("[sim] FaceEmbedder init failed (%s) — sim mode", exc)
            self._sim = True

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    # ── Public API ───────────────────────────────────────────────────────

    def embed(
        self,
        frame: np.ndarray,
        landmarks: List[Tuple[int, int]],
    ) -> np.ndarray:
        """Return a 512-dim L2-normalised embedding for the face in *frame*.

        Parameters
        ----------
        frame:
            Full camera frame (H×W×3 BGR uint8).
        landmarks:
            5 × (x, y) pixel coordinates in SCRFD order:
            left_eye, right_eye, nose, left_mouth, right_mouth.

        Returns
        -------
        np.ndarray
            Shape (512,), dtype float32. Zero vector in sim mode or on error.
        """
        zero = np.zeros(_EMBED_DIM, dtype=np.float32)
        if self._sim or self._engine is None:
            return zero

        try:
            crop = self._align(frame, landmarks)
        except Exception as exc:
            log.warning("Face alignment failed: %s", exc)
            return zero

        try:
            outputs = self._engine.infer({self._input_name: crop})
        except Exception as exc:
            log.warning("ArcFace infer failed: %s", exc)
            return zero

        raw = outputs.get(self._output_name)
        if raw is None:
            log.warning("ArcFace output key %r not in %s", self._output_name, list(outputs))
            return zero

        emb = raw.flatten().astype(np.float32)
        if emb.shape[0] == 0:
            return zero
        return _l2_normalize(emb)

    def close(self) -> None:
        if self._engine is not None:
            try:
                self._engine.close()
            except Exception:
                pass
            self._engine = None

    # ── Internal ─────────────────────────────────────────────────────────

    def _align(
        self,
        frame: np.ndarray,
        landmarks: List[Tuple[int, int]],
    ) -> np.ndarray:
        """Affine-warp *frame* so that the face landmarks align to the
        112×112 ArcFace reference positions.  Returns a (112, 112, 3) uint8
        BGR array ready for inference.
        """
        src = np.array(landmarks[:5], dtype=np.float32)
        # NOTE: keep LMEDS here — switching to other methods (or 0) degrades
        # the 5-point similarity fit and breaks ArcFace recognition (every face
        # gets a different embedding even across consecutive frames).
        M, _ = cv2.estimateAffinePartial2D(src, _ARCFACE_REF, method=cv2.LMEDS)
        if M is None:
            # Fallback: rough crop centred on the bounding box
            raise ValueError("estimateAffinePartial2D returned None")
        aligned = cv2.warpAffine(frame, M, (112, 112), flags=cv2.INTER_LINEAR)
        return aligned


def _l2_normalize(v: np.ndarray) -> np.ndarray:
    norm = np.linalg.norm(v)
    if norm < 1e-10:
        return v
    return v / norm
