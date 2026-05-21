"""Depth estimation utilities for the Desktop Assistant.

Three methods are provided:

1. **Face-size depth** — uses the known average frontal face width (~14.5 cm)
   combined with the camera focal length (derived from FOV + frame width) to
   estimate distance.  No calibration required; accurate to ±15% for frontal
   faces at 0.3–4 m.

   Z = (focal_px × face_width_m) / bbox_width_px

2. **Sparse stereo disparity depth** — uses the horizontal displacement of a
   matched feature (face centroid or template) between two horizontally-offset
   cameras.  Implemented by ``StereoFaceMatcher``.

   Z = (focal_px × baseline_m) / |disparity_px|

3. **Dense stereo disparity (SGBM)** — runs OpenCV ``StereoSGBM`` on the full
   frame pair to produce a per-pixel depth map.  Optionally uses a calibration
   file (``config/stereo_cal.npz``) to produce metric depths in metres via the
   disparity-to-depth matrix Q.  Implemented by ``DenseStereoMatcher``.

All methods return depth in metres.  The caller is responsible for combining
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


# ---------------------------------------------------------------------------
# Dense stereo (SGBM)
# ---------------------------------------------------------------------------

class DenseStereoMatcher:
    """Per-pixel depth map from a rectified stereo frame pair using SGBM.

    When a calibration Q matrix is available (passed in or loaded from
    ``config/stereo_cal.npz``), depths are in absolute metres.  Without
    calibration, the output is a normalised inverse-depth map scaled so that
    the minimum meaningful disparity maps to approximately ``max_depth_m``.

    Parameters
    ----------
    Q:
        4×4 disparity-to-depth matrix from ``cv2.stereoRectify``.  Pass *None*
        to use the uncalibrated fallback (results are relative, not metric).
    focal_px, baseline_m:
        Used only in uncalibrated mode to compute approximate metric depths.
    proc_width, proc_height:
        Resolution to process at.  Frames are resized to this before SGBM.
        640×480 is a good default for Pi 5 real-time use.
    num_disparities:
        Must be divisible by 16.  Larger values find farther objects but cost
        more CPU.  128 → covers objects from ~0.3 m to ~4 m at 56 mm baseline.
    block_size:
        Matched block size (odd number 3–11).  Larger = smoother but less
        detail.
    min_depth_m, max_depth_m:
        Depth values outside this range are set to NaN in the output.
    """

    def __init__(
        self,
        Q: Optional[np.ndarray] = None,
        focal_px: float = 800.0,
        baseline_m: float = 0.056,
        proc_width: int = 640,
        proc_height: int = 480,
        num_disparities: int = 128,
        block_size: int = 5,
        min_depth_m: float = 0.25,
        max_depth_m: float = 6.0,
    ) -> None:
        self._Q = Q
        self._focal_px = focal_px
        self._baseline_m = baseline_m
        self._proc_w = proc_width
        self._proc_h = proc_height
        self._min_d = min_depth_m
        self._max_d = max_depth_m

        # Ensure num_disparities is a positive multiple of 16
        num_disparities = max(16, (num_disparities // 16) * 16)

        self._sgbm = cv2.StereoSGBM_create(
            minDisparity=0,
            numDisparities=num_disparities,
            blockSize=block_size,
            P1=8 * 3 * block_size ** 2,
            P2=32 * 3 * block_size ** 2,
            disp12MaxDiff=1,
            uniquenessRatio=10,
            speckleWindowSize=100,
            speckleRange=32,
            preFilterCap=63,
            mode=cv2.STEREO_SGBM_MODE_SGBM_3WAY,
        )

    def compute(
        self,
        frame1: np.ndarray,
        frame2: np.ndarray,
    ) -> np.ndarray:
        """Compute a per-pixel depth map from a rectified stereo pair.

        Parameters
        ----------
        frame1, frame2:
            BGR or grayscale frames from cam1 and cam2.  Should be rectified
            (passed through ``StereoRectifier.rectify``) for best results.

        Returns
        -------
        depth_m : np.ndarray, shape (H, W), dtype float32
            Per-pixel depth in metres.  Invalid/occluded pixels are NaN.
            H × W match ``proc_height`` × ``proc_width``.
        """
        def _prep(frame: np.ndarray) -> np.ndarray:
            if frame.ndim == 3:
                frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            if (frame.shape[1], frame.shape[0]) != (self._proc_w, self._proc_h):
                frame = cv2.resize(frame, (self._proc_w, self._proc_h),
                                   interpolation=cv2.INTER_AREA)
            return frame

        g1 = _prep(frame1)
        g2 = _prep(frame2)

        # SGBM disparity (fixed-point ×16)
        disp16 = self._sgbm.compute(g1, g2).astype(np.float32)
        disp = disp16 / 16.0

        # Mask invalid disparities (SGBM fills with minDisparity - 1 = -1)
        valid = disp > 0.5

        if self._Q is not None:
            # Metric depth via calibration Q matrix
            disp_full = np.zeros_like(disp)
            disp_full[valid] = disp[valid]
            points = cv2.reprojectImageTo3D(disp_full, self._Q)
            depth_m = points[:, :, 2].copy()
            depth_m[~valid] = np.nan
        else:
            # Uncalibrated fallback: Z = focal × baseline / disparity
            depth_m = np.full_like(disp, np.nan)
            depth_m[valid] = (self._focal_px * self._baseline_m) / disp[valid]

        # Clamp to configured range
        depth_m[(depth_m < self._min_d) | (depth_m > self._max_d)] = np.nan
        return depth_m

    def summary(self, depth_m: np.ndarray) -> dict:
        """Return scalar statistics from a depth map for bus publishing or logging."""
        valid = depth_m[~np.isnan(depth_m)]
        if valid.size == 0:
            return {"nearest_m": None, "farthest_m": None, "mean_m": None, "valid_pct": 0.0}
        return {
            "nearest_m": round(float(np.nanmin(depth_m)), 3),
            "farthest_m": round(float(np.nanmax(depth_m)), 3),
            "mean_m": round(float(np.nanmean(depth_m)), 3),
            "valid_pct": round(float(valid.size / depth_m.size * 100), 1),
        }

