"""Tests for RoomService."""

import json
import threading
import time
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from src.core.bus import MessageBus
from src.services.room_service import (
    RoomService,
    _bhattacharyya,
    _compute_signature,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_service(
    tmp_path: Path,
    vision_svc=None,
    room_name: Optional[str] = None,
) -> RoomService:
    bus = MessageBus()
    state_path = tmp_path / "room_state.json"
    if room_name is not None:
        state_path.write_text(json.dumps({"name": room_name, "signature": None}))
    svc = RoomService(bus=bus, vision_service=vision_svc, state_path=state_path)
    return svc


# ── Pure function tests ───────────────────────────────────────────────────────


class TestComputeSignature:
    def test_returns_float_array(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        sig = _compute_signature(frame)
        assert isinstance(sig, np.ndarray)
        assert sig.dtype == float

    def test_length_is_32(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        sig = _compute_signature(frame)
        assert len(sig) == 32

    def test_normalized(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        sig = _compute_signature(frame)
        assert abs(sig.sum() - 1.0) < 1e-6

    def test_grayscale_input(self):
        frame = np.full((480, 640), 128, dtype=np.uint8)
        sig = _compute_signature(frame)
        assert len(sig) == 32

    def test_different_brightness_different_signature(self):
        dark = np.zeros((480, 640, 3), dtype=np.uint8)
        bright = np.full((480, 640, 3), 200, dtype=np.uint8)
        sig_dark = _compute_signature(dark)
        sig_bright = _compute_signature(bright)
        assert not np.allclose(sig_dark, sig_bright)


class TestBhattacharyya:
    def test_identical_distributions_score_one(self):
        h = np.ones(32) / 32
        assert _bhattacharyya(h, h) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_distributions_score_zero(self):
        h1 = np.zeros(32)
        h1[0] = 1.0
        h2 = np.zeros(32)
        h2[-1] = 1.0
        assert _bhattacharyya(h1, h2) == pytest.approx(0.0, abs=1e-6)

    def test_similar_signatures_high_score(self):
        rng = np.random.default_rng(42)
        base = rng.integers(80, 120, (480, 640, 3), dtype=np.uint8)
        # Add a small offset — similar overall brightness distribution
        noise = rng.integers(0, 15, (480, 640, 3), dtype=np.uint8)
        frame1 = base
        frame2 = np.clip(base.astype(int) + noise, 0, 255).astype(np.uint8)
        sig1 = _compute_signature(frame1)
        sig2 = _compute_signature(frame2)
        assert _bhattacharyya(sig1, sig2) > 0.85

    def test_different_signatures_lower_score(self):
        frame1 = np.full((480, 640, 3), 20, dtype=np.uint8)   # dark room
        frame2 = np.full((480, 640, 3), 220, dtype=np.uint8)  # bright room
        sig1 = _compute_signature(frame1)
        sig2 = _compute_signature(frame2)
        assert _bhattacharyya(sig1, sig2) < 0.5


# ── RoomService lifecycle ─────────────────────────────────────────────────────


class TestRoomServiceLifecycle:
    def test_start_stop(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        assert svc.is_running()
        svc.stop()
        assert not svc.is_running()

    def test_room_name_none_on_fresh_start(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            assert svc.room_name is None
        finally:
            svc.stop()

    def test_loads_persisted_room_name(self, tmp_path):
        svc = _make_service(tmp_path, room_name="Kitchen")
        svc.start()
        try:
            assert svc.room_name == "Kitchen"
        finally:
            svc.stop()


# ── Room set handler ──────────────────────────────────────────────────────────


class TestRoomSetHandler:
    def test_set_room_via_bus(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("room.set", {"name": "Living Room"})
            deadline = time.monotonic() + 2.0
            while svc.room_name != "Living Room" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert svc.room_name == "Living Room"
        finally:
            svc.stop()

    def test_set_room_publishes_updated_event(self, tmp_path):
        svc = _make_service(tmp_path)
        received: list = []
        svc.start()
        try:
            svc.bus.subscribe("room.updated", lambda _t, p: received.append(p))
            svc.bus.publish("room.set", {"name": "Bedroom"})
            deadline = time.monotonic() + 2.0
            while not received and time.monotonic() < deadline:
                time.sleep(0.05)
            assert received
            assert received[0]["name"] == "Bedroom"
        finally:
            svc.stop()

    def test_set_room_persists_to_disk(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("room.set", {"name": "Office"})
            deadline = time.monotonic() + 2.0
            while svc.room_name != "Office" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert svc.room_name == "Office"
        finally:
            svc.stop()
        state_path = tmp_path / "room_state.json"
        data = json.loads(state_path.read_text())
        assert data["name"] == "Office"

    def test_set_room_resets_divergence_counter(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc._consec_diverged = 5
            svc.bus.publish("room.set", {"name": "Study"})
            deadline = time.monotonic() + 2.0
            while svc.room_name != "Study" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert svc._consec_diverged == 0
        finally:
            svc.stop()

    def test_ignores_empty_name(self, tmp_path):
        svc = _make_service(tmp_path, room_name="Garage")
        svc.start()
        try:
            svc.bus.publish("room.set", {"name": "  "})
            time.sleep(0.2)
            assert svc.room_name == "Garage"  # unchanged
        finally:
            svc.stop()

    def test_ignores_non_dict_payload(self, tmp_path):
        svc = _make_service(tmp_path, room_name="Hallway")
        svc.start()
        try:
            svc.bus.publish("room.set", "not a dict")
            time.sleep(0.2)
            assert svc.room_name == "Hallway"
        finally:
            svc.stop()


# ── Visual signature capture ──────────────────────────────────────────────────


class TestSignatureCapture:
    def test_returns_none_when_no_vision_service(self, tmp_path):
        svc = _make_service(tmp_path, vision_svc=None)
        assert svc._capture_signature() is None

    def test_returns_none_when_latest_frame_is_none(self, tmp_path):
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = None
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        assert svc._capture_signature() is None

    def test_returns_signature_when_frame_available(self, tmp_path):
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        sig = svc._capture_signature()
        assert sig is not None
        assert len(sig) == 32

    def test_handles_vision_exception_gracefully(self, tmp_path):
        mock_vis = MagicMock()
        mock_vis.latest_frame.side_effect = RuntimeError("camera gone")
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        # Should not raise
        sig = svc._capture_signature()
        assert sig is None


# ── State persistence ─────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_load_state_with_signature(self, tmp_path):
        sig = [0.03125] * 32
        state_path = tmp_path / "room_state.json"
        state_path.write_text(json.dumps({"name": "Dining Room", "signature": sig}))
        svc = RoomService(
            bus=MessageBus(), vision_service=None, state_path=state_path
        )
        svc._load_state()
        assert svc.room_name == "Dining Room"
        assert svc._baseline_sig is not None
        assert len(svc._baseline_sig) == 32

    def test_load_state_handles_missing_file(self, tmp_path):
        state_path = tmp_path / "nonexistent.json"
        svc = RoomService(
            bus=MessageBus(), vision_service=None, state_path=state_path
        )
        svc._load_state()  # must not raise
        assert svc.room_name is None

    def test_load_state_handles_corrupt_file(self, tmp_path):
        state_path = tmp_path / "room_state.json"
        state_path.write_text("{bad json}")
        svc = RoomService(
            bus=MessageBus(), vision_service=None, state_path=state_path
        )
        svc._load_state()  # must not raise
        assert svc.room_name is None

    def test_save_and_reload(self, tmp_path):
        state_path = tmp_path / "room_state.json"
        svc = RoomService(
            bus=MessageBus(), vision_service=None, state_path=state_path
        )
        svc._room_name = "Garage"
        svc._baseline_sig = np.ones(32) / 32
        svc._save_state()

        svc2 = RoomService(
            bus=MessageBus(), vision_service=None, state_path=state_path
        )
        svc2._load_state()
        assert svc2.room_name == "Garage"
        assert svc2._baseline_sig is not None


# ── Divergence detection ──────────────────────────────────────────────────────


class TestDivergenceDetection:
    def test_establishes_baseline_on_first_check(self, tmp_path):
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = np.full((480, 640, 3), 100, dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        svc.start()
        try:
            assert svc._baseline_sig is None
            svc._check_scene()
            assert svc._baseline_sig is not None
        finally:
            svc.stop()

    def test_similar_scene_resets_divergence_counter(self, tmp_path):
        frame = np.full((480, 640, 3), 100, dtype=np.uint8)
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = frame
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        svc._baseline_sig = _compute_signature(frame)
        svc._consec_diverged = 2
        svc.start()
        try:
            svc._check_scene()
            assert svc._consec_diverged == 0
        finally:
            svc.stop()

    def test_different_scene_increments_divergence_counter(self, tmp_path):
        baseline_frame = np.full((480, 640, 3), 20, dtype=np.uint8)   # dark
        current_frame = np.full((480, 640, 3), 220, dtype=np.uint8)   # bright
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = current_frame
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        svc._baseline_sig = _compute_signature(baseline_frame)
        svc._consec_diverged = 0
        svc.start()
        try:
            svc._check_scene()
            assert svc._consec_diverged == 1
        finally:
            svc.stop()

    def test_prompt_fires_after_sustained_divergence(self, tmp_path):
        baseline_frame = np.full((480, 640, 3), 20, dtype=np.uint8)
        current_frame = np.full((480, 640, 3), 220, dtype=np.uint8)
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = current_frame
        svc = _make_service(tmp_path, vision_svc=mock_vis, room_name="Basement")
        spoken: list = []
        svc.bus.subscribe("av.say", lambda _t, p: spoken.append(p))
        svc._baseline_sig = _compute_signature(baseline_frame)
        svc._consec_diverged = 2  # one more will trigger
        svc._last_prompt_ts = float("-inf")  # cooldown fully elapsed
        svc.start()
        try:
            svc._check_scene()
            deadline = time.monotonic() + 2.0
            while not spoken and time.monotonic() < deadline:
                time.sleep(0.05)
            assert spoken
            assert "Basement" in spoken[0]["text"]
        finally:
            svc.stop()

    def test_prompt_respects_cooldown(self, tmp_path):
        baseline_frame = np.full((480, 640, 3), 20, dtype=np.uint8)
        current_frame = np.full((480, 640, 3), 220, dtype=np.uint8)
        mock_vis = MagicMock()
        mock_vis.latest_frame.return_value = current_frame
        svc = _make_service(tmp_path, vision_svc=mock_vis, room_name="Den")
        spoken: list = []
        svc.bus.subscribe("av.say", lambda _t, p: spoken.append(p))
        svc._baseline_sig = _compute_signature(baseline_frame)
        svc._consec_diverged = 2
        svc._last_prompt_ts = time.monotonic()  # just prompted — cooldown active
        svc.start()
        try:
            svc._check_scene()
            time.sleep(0.3)
            # Only the startup announcement may have fired, not a room-change prompt
            divergence_prompts = [s for s in spoken if "different" in s.get("text", "")]
            assert not divergence_prompts
        finally:
            svc.stop()
