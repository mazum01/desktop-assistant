"""Tests for MonoDepthService."""

from __future__ import annotations

import time
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


_MODEL_H, _MODEL_W = 256, 320


def _make_service(bus=None, scale_factor=None, sim_engine=True):
    from src.services.mono_depth_service import MonoDepthService
    cfg = {
        "depth": {
            "mono_rate_hz": 3.0,
            "mono_hef_path": "/usr/local/hailo/resources/models/hailo8/scdepthv3.hef",
            "mono_scale_factor": scale_factor,
            "min_depth_m": 0.25,
            "max_depth_m": 6.0,
        }
    }
    svc = MonoDepthService(bus=bus or MagicMock(), config=cfg)
    if sim_engine:
        # Inject a mock engine that returns synthetic uint16 output
        mock_engine = MagicMock()
        mock_engine.hardware_ready = False

        def fake_infer(inputs):
            raw = np.random.randint(0, 65535, (_MODEL_H, _MODEL_W, 1), dtype=np.uint16)
            return {"scdepthv3/conv31": raw}

        mock_engine.infer.side_effect = fake_infer
        svc._engine = mock_engine
        # Also make on_start a no-op since engine is already set
        svc._stop_event.clear()
    return svc


class TestMonoDepthService:

    def test_latest_payload_initially_none(self):
        from src.services.mono_depth_service import MonoDepthService
        svc = MonoDepthService(bus=MagicMock())
        assert svc.latest_payload() is None

    def test_process_frame_produces_payload(self):
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        assert payload is not None

    def test_payload_has_required_keys(self):
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        for key in ("depth_rel", "width", "height", "nearest_rel", "farthest_rel", "ts"):
            assert key in payload, f"Missing key: {key}"

    def test_output_dimensions(self):
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        assert payload["width"] == _MODEL_W
        assert payload["height"] == _MODEL_H
        assert len(payload["depth_rel"]) == _MODEL_H
        assert len(payload["depth_rel"][0]) == _MODEL_W

    def test_depth_rel_normalised(self):
        """depth_rel values should be in [0, 1]."""
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        flat = [v for row in payload["depth_rel"] for v in row]
        assert all(0.0 <= v <= 1.0 for v in flat), "depth_rel out of [0,1] range"

    def test_nearest_farthest_rel_range(self):
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        assert 0.0 <= payload["farthest_rel"] <= payload["nearest_rel"] <= 1.0

    def test_no_metric_depth_without_scale_factor(self):
        svc = _make_service(scale_factor=None)
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        assert payload["depth_m"] == []
        assert payload["scale_factor"] is None

    def test_metric_depth_with_scale_factor(self):
        svc = _make_service(scale_factor=2.0)
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        payload = svc.latest_payload()
        assert len(payload["depth_m"]) == _MODEL_H
        assert payload["scale_factor"] == 2.0
        flat = [v for row in payload["depth_m"] for v in row]
        assert all(0.25 <= v <= 6.0 for v in flat), "metric depth out of configured range"

    def test_payload_json_serialisable(self):
        import json
        svc = _make_service()
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        # Should not raise
        json.dumps(svc.latest_payload())

    def test_bus_publishes_on_process(self):
        bus = MagicMock()
        svc = _make_service(bus=bus)
        frame = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
        svc._process_frame(frame)
        bus.publish.assert_called_once()
        topic, payload = bus.publish.call_args[0]
        assert topic == "vision.mono_depth_map"

    def test_start_stop_no_frames(self):
        """Service should start and stop without frames without crashing."""
        from src.services.mono_depth_service import MonoDepthService
        svc = MonoDepthService(bus=MagicMock())
        svc._engine = MagicMock()
        svc._engine.hardware_ready = False
        svc._stop_event.clear()
        import threading
        svc._thread = threading.Thread(target=svc._run_loop, daemon=True)
        svc._thread.start()
        time.sleep(0.1)
        svc._stop_event.set()
        svc._thread.join(timeout=2)

    def test_config_dict_parsing(self):
        """Dict config should be parsed correctly."""
        from src.services.mono_depth_service import MonoDepthService
        svc = MonoDepthService(config={"depth": {
            "mono_rate_hz": 5.0,
            "mono_scale_factor": 1.5,
            "min_depth_m": 0.3,
            "max_depth_m": 8.0,
        }})
        assert svc._cfg.rate_hz == 5.0
        assert svc._cfg.scale_factor == 1.5
        assert svc._cfg.min_depth_m == 0.3
        assert svc._cfg.max_depth_m == 8.0
