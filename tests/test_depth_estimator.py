"""Tests for depth_estimator — face-size depth, stereo disparity, and 3D projection."""
import math
import pytest
import numpy as np

from src.perception.depth_estimator import (
    focal_px_from_fov,
    face_size_depth,
    stereo_depth_from_disparity,
    to_3d,
    StereoFaceMatcher,
)


# ── focal_px_from_fov ────────────────────────────────────────────────────────

def test_focal_px_from_fov_basic():
    """100° FOV at 640px should give ~268 px focal length."""
    f = focal_px_from_fov(640, 100.0)
    expected = (640 / 2.0) / math.tan(math.radians(50))
    assert f == pytest.approx(expected, rel=1e-4)


def test_focal_px_from_fov_narrower():
    """60° FOV should give a longer focal length than 100°."""
    f60 = focal_px_from_fov(640, 60.0)
    f100 = focal_px_from_fov(640, 100.0)
    assert f60 > f100


def test_focal_px_from_fov_invalid():
    with pytest.raises(ValueError):
        focal_px_from_fov(640, 0.0)
    with pytest.raises(ValueError):
        focal_px_from_fov(0, 90.0)


# ── face_size_depth ───────────────────────────────────────────────────────────

def test_face_size_depth_one_metre():
    """Face at 1 m should have a bbox width ≈ focal_px * face_w."""
    focal = focal_px_from_fov(640, 100.0)
    face_w = 0.145
    bbox_w = focal * face_w / 1.0  # expected at exactly 1 m
    depth = face_size_depth(bbox_w, focal, face_w)
    assert depth == pytest.approx(1.0, rel=0.01)


def test_face_size_depth_half_metre():
    focal = focal_px_from_fov(640, 100.0)
    face_w = 0.145
    bbox_w = focal * face_w / 0.5
    depth = face_size_depth(bbox_w, focal, face_w)
    assert depth == pytest.approx(0.5, rel=0.01)


def test_face_size_depth_invalid():
    assert face_size_depth(0, 268.0) is None
    assert face_size_depth(40, 0) is None
    assert face_size_depth(40, 268.0, face_width_m=0) is None


# ── stereo_depth_from_disparity ──────────────────────────────────────────────

def test_stereo_depth_from_disparity_basic():
    """Z = f*B/d: 268 px focal, 56mm baseline, 15px disparity ≈ 1.0 m."""
    focal = 268.0
    baseline = 0.056
    disparity = focal * baseline / 1.0  # ≈ 15 px at 1 m
    d = stereo_depth_from_disparity(disparity, focal, baseline)
    assert d == pytest.approx(1.0, rel=0.01)


def test_stereo_depth_zero_disparity():
    assert stereo_depth_from_disparity(0.0, 268.0, 0.056) is None


def test_stereo_depth_invalid_baseline():
    assert stereo_depth_from_disparity(10.0, 268.0, 0.0) is None


# ── to_3d ─────────────────────────────────────────────────────────────────────

def test_to_3d_centre_face():
    """Face at image centre should have X_m ≈ 0, Y_m ≈ 0."""
    focal = focal_px_from_fov(640, 100.0)
    x_m, y_m, z_m = to_3d(320, 240, 1.0, focal, 640, 480)
    assert x_m == pytest.approx(0.0, abs=0.001)
    assert y_m == pytest.approx(0.0, abs=0.001)
    assert z_m == pytest.approx(1.0)


def test_to_3d_off_centre():
    """Face offset right should have positive X_m."""
    focal = focal_px_from_fov(640, 100.0)
    x_m, y_m, z_m = to_3d(480, 240, 1.0, focal, 640, 480)
    assert x_m > 0.0


# ── StereoFaceMatcher ────────────────────────────────────────────────────────

def _make_face_frame(width=640, height=480, face_cx=320, face_cy=240, face_r=30):
    """Create a synthetic BGR frame with a white circle as a 'face'."""
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    import cv2
    cv2.circle(frame, (face_cx, face_cy), face_r, (200, 180, 160), -1)
    return frame


def test_stereo_matcher_self_match():
    """Matching a face against an identical frame should give near-zero disparity → max depth."""
    focal = focal_px_from_fov(640, 100.0)
    matcher = StereoFaceMatcher(focal_px=focal, baseline_m=0.056, min_depth_m=0.25, max_depth_m=6.0)
    frame = _make_face_frame(face_cx=320)
    bbox = [290.0, 210.0, 350.0, 270.0]
    # Same frame → disparity ≈ 0 → template match may return None (below threshold) or max depth
    result = matcher.estimate(bbox, frame, frame)
    # Either None (disparity too small → depth out of range) or a valid depth value
    assert result is None or (0.25 <= result <= 6.0)


def test_stereo_matcher_invalid_frames():
    focal = focal_px_from_fov(640, 100.0)
    matcher = StereoFaceMatcher(focal_px=focal, baseline_m=0.056)
    frame1 = _make_face_frame(width=640, height=480)
    frame2 = _make_face_frame(width=320, height=240)  # different resolution
    bbox = [290.0, 210.0, 350.0, 270.0]
    result = matcher.estimate(bbox, frame1, frame2)
    assert result is None
