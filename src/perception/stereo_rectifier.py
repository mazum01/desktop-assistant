"""Stereo rectification using saved calibration data.

Loads ``config/stereo_cal.npz`` (produced by ``scripts/calibrate_stereo.py``)
and applies the computed rectification maps to frame pairs from both cameras.

When no calibration file exists the class operates in **uncalibrated mode**:
``rectify()`` returns the original frames unchanged so downstream code still
works — it just won't benefit from epipolar alignment.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_CAL_PATH = _REPO_ROOT / "config" / "stereo_cal.npz"


class StereoRectifier:
    """Apply pre-computed stereo rectification maps to camera frame pairs.

    Parameters
    ----------
    cal_path:
        Path to the .npz calibration file.  If *None* (the default), the
        standard ``config/stereo_cal.npz`` path is used.
    """

    def __init__(self, cal_path: Optional[Path] = None) -> None:
        self._cal_path = Path(cal_path) if cal_path else _DEFAULT_CAL_PATH
        self._map1x: Optional[np.ndarray] = None
        self._map1y: Optional[np.ndarray] = None
        self._map2x: Optional[np.ndarray] = None
        self._map2y: Optional[np.ndarray] = None
        self._Q: Optional[np.ndarray] = None
        self._image_size: Optional[tuple[int, int]] = None
        self._calibrated: bool = False
        self._rms: Optional[float] = None
        self._load()

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def calibrated(self) -> bool:
        """True when a valid calibration file was loaded."""
        return self._calibrated

    @property
    def Q(self) -> Optional[np.ndarray]:
        """Disparity-to-depth mapping matrix (4×4). None if uncalibrated."""
        return self._Q

    @property
    def rms(self) -> Optional[float]:
        """Reprojection RMS error from calibration run. None if uncalibrated."""
        return self._rms

    @property
    def image_size(self) -> Optional[tuple[int, int]]:
        """(width, height) the calibration was run at. None if uncalibrated."""
        return self._image_size

    # ── Loading ─────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._cal_path.exists():
            log.info(
                "StereoRectifier: no calibration file at %s — running in uncalibrated mode",
                self._cal_path,
            )
            return
        try:
            data = np.load(self._cal_path)
            self._map1x = data["map1x"]
            self._map1y = data["map1y"]
            self._map2x = data["map2x"]
            self._map2y = data["map2y"]
            self._Q = data["Q"]
            sz = data["image_size"]
            self._image_size = (int(sz[0]), int(sz[1]))
            self._rms = float(data.get("rms", 0.0))
            self._calibrated = True
            log.info(
                "StereoRectifier loaded calibration from %s (size=%dx%d, rms=%.4f)",
                self._cal_path, self._image_size[0], self._image_size[1], self._rms,
            )
        except Exception:
            log.exception("StereoRectifier: failed to load calibration from %s", self._cal_path)

    def reload(self) -> bool:
        """Reload calibration file from disk. Returns True on success."""
        self._calibrated = False
        self._map1x = self._map1y = self._map2x = self._map2y = None
        self._Q = self._image_size = self._rms = None
        self._load()
        return self._calibrated

    # ── Core operation ───────────────────────────────────────────────────

    def rectify(
        self,
        frame1: np.ndarray,
        frame2: np.ndarray,
    ) -> tuple[np.ndarray, np.ndarray]:
        """Apply rectification to a stereo frame pair.

        In uncalibrated mode the frames are returned unchanged.
        Both frames are resized to the calibration resolution if needed.
        """
        if not self._calibrated or self._map1x is None:
            return frame1, frame2

        cal_w, cal_h = self._image_size  # type: ignore[misc]

        def _prepare(frame: np.ndarray) -> np.ndarray:
            h, w = frame.shape[:2]
            if (w, h) != (cal_w, cal_h):
                frame = cv2.resize(frame, (cal_w, cal_h), interpolation=cv2.INTER_LINEAR)
            return frame

        f1 = _prepare(frame1)
        f2 = _prepare(frame2)

        r1 = cv2.remap(f1, self._map1x, self._map1y, cv2.INTER_LINEAR)
        r2 = cv2.remap(f2, self._map2x, self._map2y, cv2.INTER_LINEAR)
        return r1, r2

    def reproject_to_3d(self, disparity: np.ndarray) -> Optional[np.ndarray]:
        """Reproject a disparity map to 3D coordinates using matrix Q.

        Returns an (H, W, 3) array of (X, Y, Z) in metres, or None if
        uncalibrated.
        """
        if self._Q is None:
            return None
        points_3d = cv2.reprojectImageTo3D(disparity.astype(np.float32), self._Q)
        return points_3d
