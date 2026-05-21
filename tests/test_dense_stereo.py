"""Tests for DenseStereoMatcher, StereoRectifier, and DenseStereoService."""

from __future__ import annotations

import numpy as np
import pytest
import threading
import time
from unittest.mock import MagicMock, patch


# ── DenseStereoMatcher ────────────────────────────────────────────────────────

class TestDenseStereoMatcher:
    def _make_matcher(self, Q=None):
        from src.perception.depth_estimator import DenseStereoMatcher
        return DenseStereoMatcher(
            Q=Q,
            focal_px=268.0,
            baseline_m=0.056,
            proc_width=320,
            proc_height=240,
            num_disparities=64,
            block_size=5,
        )

    def _make_pair(self, w=320, h=240):
        rng = np.random.default_rng(42)
        base = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
        # Shift second frame slightly to simulate disparity
        shifted = np.zeros_like(base)
        shift = 8
        shifted[:, shift:] = base[:, :-shift]
        return base, shifted

    def test_compute_returns_float32(self):
        matcher = self._make_matcher()
        f1, f2 = self._make_pair()
        result = matcher.compute(f1, f2)
        assert isinstance(result, np.ndarray)
        assert result.dtype == np.float32

    def test_compute_shape_matches_proc_dims(self):
        matcher = self._make_matcher()
        f1, f2 = self._make_pair()
        result = matcher.compute(f1, f2)
        assert result.shape == (240, 320)

    def test_summary_keys(self):
        matcher = self._make_matcher()
        f1, f2 = self._make_pair()
        dm = matcher.compute(f1, f2)
        s = matcher.summary(dm)
        assert "nearest_m" in s
        assert "farthest_m" in s
        assert "mean_m" in s
        assert "valid_pct" in s

    def test_summary_valid_pct_range(self):
        matcher = self._make_matcher()
        f1, f2 = self._make_pair()
        dm = matcher.compute(f1, f2)
        s = matcher.summary(dm)
        assert 0.0 <= s["valid_pct"] <= 100.0

    def test_all_nan_depth_map(self):
        matcher = self._make_matcher()
        dm = np.full((240, 320), np.nan, dtype=np.float32)
        s = matcher.summary(dm)
        assert s["nearest_m"] is None
        assert s["farthest_m"] is None
        assert s["mean_m"] is None
        assert s["valid_pct"] == 0.0

    def test_mismatched_frame_sizes_handled(self):
        """Frames of different sizes should not raise — matcher resizes."""
        matcher = self._make_matcher()
        f1 = np.zeros((480, 640, 3), dtype=np.uint8)
        f2 = np.zeros((480, 640, 3), dtype=np.uint8)
        result = matcher.compute(f1, f2)
        assert result.shape == (240, 320)


# ── StereoRectifier ────────────────────────────────────────────────────────────

class TestStereoRectifier:
    def test_loads_gracefully_without_calibration(self, tmp_path):
        from src.perception.stereo_rectifier import StereoRectifier
        r = StereoRectifier(cal_path=str(tmp_path / "nonexistent.npz"))
        assert r.calibrated is False

    def test_rectify_noop_when_uncalibrated(self, tmp_path):
        from src.perception.stereo_rectifier import StereoRectifier
        r = StereoRectifier(cal_path=str(tmp_path / "nonexistent.npz"))
        f1 = np.zeros((240, 320, 3), dtype=np.uint8)
        f2 = np.zeros((240, 320, 3), dtype=np.uint8)
        r1, r2 = r.rectify(f1, f2)
        # Uncalibrated rectify returns the originals unchanged
        np.testing.assert_array_equal(r1, f1)
        np.testing.assert_array_equal(r2, f2)

    def test_Q_none_when_uncalibrated(self, tmp_path):
        from src.perception.stereo_rectifier import StereoRectifier
        r = StereoRectifier(cal_path=str(tmp_path / "nonexistent.npz"))
        assert r.Q is None

    def test_rms_none_when_uncalibrated(self, tmp_path):
        from src.perception.stereo_rectifier import StereoRectifier
        r = StereoRectifier(cal_path=str(tmp_path / "nonexistent.npz"))
        assert r.rms is None


# ── DenseStereoService ─────────────────────────────────────────────────────────

class TestDenseStereoService:
    def _make_service(self, bus=None, cfg_override=None):
        from src.services.dense_stereo_service import DenseStereoService
        cfg = {
            "depth": {
                "dense_enabled": True,
                "dense_rate_hz": 3.0,
                "dense_width": 320,
                "dense_height": 240,
                "num_disparities": 64,
                "block_size": 5,
                "focal_px": 268.0,
                "baseline_m": 0.056,
                "min_depth_m": 0.25,
                "max_depth_m": 6.0,
            }
        }
        if cfg_override:
            cfg["depth"].update(cfg_override)
        svc = DenseStereoService(bus=bus or MagicMock(), config=cfg)
        svc.on_start()  # initialise rectifier + matcher without starting the thread
        return svc

    def test_latest_payload_initially_none(self):
        from src.services.dense_stereo_service import DenseStereoService
        svc = DenseStereoService(bus=MagicMock())
        # Before on_start, payload is None
        assert svc.latest_payload() is None

    def test_start_stop_no_frames(self):
        """Service should start and stop cleanly even with no frame input."""
        from src.services.dense_stereo_service import DenseStereoService
        svc = DenseStereoService(bus=MagicMock())
        svc.on_start()
        time.sleep(0.1)
        svc.on_stop()

    def test_process_frame_pair_publishes(self):
        """Injecting a frame pair should produce a payload on the bus."""
        bus = MagicMock()
        svc = self._make_service(bus=bus)

        rng = np.random.default_rng(7)
        f1 = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        f2 = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)

        svc._process_pair(f1, f2)

        payload = svc.latest_payload()
        assert payload is not None
        assert "depth_m" in payload
        assert "nearest_m" in payload
        assert "ts" in payload

    def test_payload_serialisable(self):
        """latest_payload must be JSON-serialisable (no ndarray values)."""
        import json
        svc = self._make_service()
        rng = np.random.default_rng(9)
        f1 = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        f2 = rng.integers(0, 255, (240, 320, 3), dtype=np.uint8)
        svc._process_pair(f1, f2)
        payload = svc.latest_payload()
        # Should not raise
        json.dumps(payload)
