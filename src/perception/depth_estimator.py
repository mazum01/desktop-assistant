"""Depth estimation utilities for the Desktop Assistant.

Two methods are provided:

1. **Face-size depth** — uses the known average frontal face width (~14.5 cm)
   combined with the camera focal length (derived from FOV + frame width) to
   estimate distance.  No calibration required; accurate to ±15% for frontal
   faces at 0.3–4 m.

   Z = (focal_px × face_width_m) / bbox_width_px

2. **Stereo disparity depth** — uses the horizontal displacement of a matched
   feature (face centroid or template) between two horizontally-offset cameras.

   Z = (focal_px × baseline_m) / |disparity_px|

Both methods return depth in metres.  The caller is responsible for combining
or selecting between them.

3D localisation:
   X_m = (cx - frame_cx) × Z / focal_px
   Y_m = (cy - frame_cy) × Z / focal_px
   Z_m = Z
"""

from __future__ import annotations

import logging
import math
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

# Default average frontal face width in metres (literature: 13–16 cm; 14.5 cm used)
DEFAULT_FACE_WIDTH_M: float = 0.145


def focal_px_from_fov(frame_width: int, fov_deg: float) -> float:
    """Compute focal length in pixels from frame width and horizontal FOV."""
    if fov_deg <= 0 or frame_width <= 0:
        raise ValueError(f"Invalid fov_deg={fov_deg} or frame_width={frame_width}")
    return (frame_width / 2.0) / math.tan(math.radians(fov_deg / 2.0))


def face_size_depth(
    bbox_width_px: float,
    focal_px: float,
    face_width_m: float = DEFAULT_FACE_WIDTH_M,
) -> Optional[float]:
    """Estimate depth from face bounding-box width.

    Returns depth in metres, or None if inputs are invalid.
    """
    if bbox_width_px <= 0 or focal_px <= 0 or face_width_m <= 0:
        return None
    return (focal_px * face_width_m) / bbox_width_px


def stereo_depth_from_disparity(
    disparity_px: float,
    focal_px: float,
    baseline_m: float,
) -> Optional[float]:
    """Compute depth from a measured horizontal disparity.

    Returns depth in metres, or None if disparity is zero or inputs invalid.
    """
    if abs(disparity_px) < 0.5 or focal_px <= 0 or baseline_m <= 0:
        return None
    return (focal_px * baseline_m) / abs(disparity_px)


def to_3d(
    cx: float,
    cy: float,
    depth_m: float,
    focal_px: float,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float, float]:
    """Back-project a 2D centroid + depth into 3D camera coordinates.

    Returns (X_m, Y_m, Z_m) where Z is forward, X is rightward, Y is downward.
    """
    x_m = (cx - frame_width / 2.0) * depth_m / focal_px
    y_m = (cy - frame_height / 2.0) * depth_m / focal_px
    return x_m, y_m, depth_m


class StereoFaceMatcher:
    """Find the horizontal disparity between a face detected in cam1 and its
    corresponding location in cam2 using template matching.

    The two cameras are assumed to be horizontally aligned with ``baseline_m``
    separation (cam1 on the left, cam2 on the right, or vice versa — sign of
    the resulting depth does not change).  No rectification is applied; the
    method works well when cameras are physically aligned.
    """

    def __init__(
        self,
        focal_px: float,
        baseline_m: float,
        min_depth_m: float = 0.25,
        max_depth_m: float = 6.0,
        template_scale: float = 0.6,
    ) -> None:
        self._focal_px = focal_px
        self._baseline_m = baseline_m
        self._min_depth_m = min_depth_m
        self._max_depth_m = max_depth_m
        self._template_scale = template_scale  # fraction of bbox to use as template

    def estimate(
        self,
        face_bbox: list[float],
        frame1: np.ndarray,
        frame2: np.ndarray,
    ) -> Optional[float]:
        """Estimate depth for a face detected in frame1 using frame2.

        ``face_bbox`` is [x1, y1, x2, y2] in frame1 pixel coords.
        Both frames must be the same resolution.
        Returns depth in metres or None on failure.
        """
        h1, w1 = frame1.shape[:2]
        h2, w2 = frame2.shape[:2]
        if h1 != h2 or w1 != w2:
            return None

        x1, y1, x2, y2 = [int(v) for v in face_bbox]
        x1, x2 = max(0, x1), min(w1, x2)
        y1, y2 = max(0, y1), min(h1, y2)
        if x2 - x1 < 8 or y2 - y1 < 8:
            return None

        # Shrink template to the central portion of the bbox to reduce edge effects
        pad_x = int((x2 - x1) * (1 - self._template_scale) / 2)
        pad_y = int((y2 - y1) * (1 - self._template_scale) / 2)
        tx1, ty1 = x1 + pad_x, y1 + pad_y
        tx2, ty2 = x2 - pad_x, y2 - pad_y
        if tx2 - tx1 < 4 or ty2 - ty1 < 4:
            return None

        # Convert to grayscale for template matching
        g1 = cv2.cvtColor(frame1, cv2.COLOR_BGR2GRAY) if frame1.ndim == 3 else frame1
        g2 = cv2.cvtColor(frame2, cv2.COLOR_BGR2GRAY) if frame2.ndim == 3 else frame2

        template = g1[ty1:ty2, tx1:tx2]

        # Compute the maximum possible disparity for the configured depth range
        max_disp = int(self._focal_px * self._baseline_m / self._min_depth_m) + 10
        # Search strip: same Y band, extended horizontally
        sx1 = max(0, x1 - max_disp)
        sx2 = min(w2, x2 + max_disp)
        sy1, sy2 = y1, y2
        if sx2 - sx1 < tx2 - tx1 or sy2 - sy1 < ty2 - ty1:
            return None

        strip = g2[sy1:sy2, sx1:sx2]
        try:
            result = cv2.matchTemplate(strip, template, cv2.TM_CCOEFF_NORMED)
        except cv2.error:
            return None

        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if max_val < 0.35:  # weak match — reject
            return None

        # Match location in frame2 coordinates
        match_x2 = sx1 + max_loc[0] + (tx2 - tx1) // 2
        cx1_face = (tx1 + tx2) // 2

        disparity = abs(cx1_face - match_x2)
        depth = stereo_depth_from_disparity(disparity, self._focal_px, self._baseline_m)
        if depth is None:
            return None
        if not (self._min_depth_m <= depth <= self._max_depth_m):
            return None
        return depth
