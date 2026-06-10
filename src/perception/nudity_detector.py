"""
NudeNet-based nudity detector wrapper.

Wraps the NudeNet NudeDetector (ONNX Runtime) and classifies a single BGR
numpy frame as "explicit" or "safe" based on a configurable set of label
categories and a confidence threshold.

Degrades gracefully to simulation mode (always returns safe) when NudeNet or
its model file is unavailable.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# Labels from NudeNet that constitute "explicit" / look-away triggers.
# Covered body parts and non-sexual exposed parts (feet, belly, armpits)
# are intentionally excluded to reduce false positives.
EXPLICIT_LABELS: frozenset[str] = frozenset({
    "FEMALE_GENITALIA_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "BUTTOCKS_EXPOSED",
    "ANUS_EXPOSED",
})

try:
    from nudenet import NudeDetector as _NudeDetector
    _NUDENET_AVAILABLE = True
except ImportError:
    _NudeDetector = None  # type: ignore
    _NUDENET_AVAILABLE = False
    log.warning("nudenet not available — NudityDetector in simulation mode")


class NudityDetector:
    """
    Single-shot nudity classification for a BGR frame.

    Parameters
    ----------
    threshold : float
        Minimum confidence score (0–1) for a detection to be counted.
    explicit_labels : set[str] | None
        Override the set of label names that trigger look-away.
        Defaults to EXPLICIT_LABELS.
    """

    def __init__(
        self,
        threshold: float = 0.6,
        explicit_labels: Optional[frozenset[str]] = None,
    ) -> None:
        self._threshold = max(0.0, min(1.0, threshold))
        self._labels = explicit_labels if explicit_labels is not None else EXPLICIT_LABELS
        self._detector = None
        self._sim = not _NUDENET_AVAILABLE

        if not self._sim:
            try:
                self._detector = _NudeDetector()
                log.info(
                    "NudityDetector ready (threshold=%.2f, labels=%s)",
                    self._threshold, sorted(self._labels),
                )
            except Exception as exc:
                log.warning("NudityDetector init failed (%s) — sim mode", exc)
                self._sim = True

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    def is_explicit(self, frame: np.ndarray) -> tuple[bool, list[dict]]:
        """
        Classify a BGR frame.

        Returns
        -------
        (explicit, detections)
            explicit    : True if any explicit detection exceeds threshold
            detections  : list of {class, score, box} dicts from NudeNet
        """
        if self._sim or self._detector is None:
            return False, []

        try:
            import cv2
            import tempfile
            import os

            # NudeNet requires a file path — write to a temp JPEG.
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
                tmp_path = f.name
            cv2.imwrite(tmp_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 85])

            try:
                detections = self._detector.detect(tmp_path)
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

            explicit = any(
                d.get("class") in self._labels and d.get("score", 0.0) >= self._threshold
                for d in detections
            )
            return explicit, detections

        except Exception as exc:
            log.debug("NudityDetector.is_explicit() error: %s", exc)
            return False, []
