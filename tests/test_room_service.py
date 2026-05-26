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
    _compare_signatures,
    _compute_brightness_signature,
    _compute_depth_signature,
    _compute_signature,  # backwards-compat alias
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
        state_path.write_text(json.dumps({"name": room_name, "brightness_sig": None}))
    svc = RoomService(bus=bus, vision_service=vision_svc, state_path=state_path)
    return svc


def _make_vision(frame: np.ndarray) -> MagicMock:
    m = MagicMock()
    m.latest_frame.return_value = frame
    return m


# ── Pure function tests ───────────────────────────────────────────────────────


class TestComputeBrightnessSignature:
    def test_returns_float_array(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        sig = _compute_brightness_signature(frame)
        assert isinstance(sig, np.ndarray)
        assert sig.dtype == float

    def test_length_is_32(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        sig = _compute_brightness_signature(frame)
        assert len(sig) == 32

    def test_normalized(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        sig = _compute_brightness_signature(frame)
        assert abs(sig.sum() - 1.0) < 1e-6

    def test_grayscale_input(self):
        frame = np.full((480, 640), 128, dtype=np.uint8)
        sig = _compute_brightness_signature(frame)
        assert len(sig) == 32

    def test_different_brightness_different_signature(self):
        dark = np.zeros((480, 640, 3), dtype=np.uint8)
        bright = np.full((480, 640, 3), 200, dtype=np.uint8)
        assert not np.allclose(_compute_brightness_signature(dark), _compute_brightness_signature(bright))

    def test_alias_compute_signature_works(self):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        assert np.allclose(_compute_signature(frame), _compute_brightness_signature(frame))


class TestComputeDepthSignature:
    def test_returns_array_of_correct_length(self):
        depth = np.random.uniform(0.5, 4.0, (480, 640))
        sig = _compute_depth_signature(depth)
        assert len(sig) == 16

    def test_normalized(self):
        depth = np.random.uniform(0.5, 4.0, (480, 640))
        sig = _compute_depth_signature(depth)
        assert abs(sig.sum() - 1.0) < 1e-6

    def test_excludes_nan_and_zero(self):
        depth = np.zeros((10, 10))
        depth[5, 5] = 2.0
        sig = _compute_depth_signature(depth)
        assert sig.sum() == pytest.approx(1.0, abs=1e-6)

    def test_all_invalid_returns_zeros(self):
        depth = np.zeros((10, 10))
        sig = _compute_depth_signature(depth)
        assert np.all(sig == 0.0)


class TestBhattacharyya:
    def test_identical_distributions_score_one(self):
        h = np.ones(32) / 32
        assert _bhattacharyya(h, h) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_distributions_score_zero(self):
        h1 = np.zeros(32); h1[0] = 1.0
        h2 = np.zeros(32); h2[-1] = 1.0
        assert _bhattacharyya(h1, h2) == pytest.approx(0.0, abs=1e-6)

    def test_similar_signatures_high_score(self):
        rng = np.random.default_rng(42)
        base = rng.integers(80, 120, (480, 640, 3), dtype=np.uint8)
        noise = rng.integers(0, 15, (480, 640, 3), dtype=np.uint8)
        frame2 = np.clip(base.astype(int) + noise, 0, 255).astype(np.uint8)
        s1 = _compute_brightness_signature(base)
        s2 = _compute_brightness_signature(frame2)
        assert _bhattacharyya(s1, s2) > 0.85

    def test_different_signatures_lower_score(self):
        s1 = _compute_brightness_signature(np.full((480, 640, 3), 20, dtype=np.uint8))
        s2 = _compute_brightness_signature(np.full((480, 640, 3), 220, dtype=np.uint8))
        assert _bhattacharyya(s1, s2) < 0.5


class TestCompareSignatures:
    def test_brightness_only_when_no_depth(self):
        h = np.ones(32) / 32
        score = _compare_signatures(h, None, h, None)
        assert score == pytest.approx(1.0, abs=1e-6)

    def test_combined_when_depth_available(self):
        hb = np.ones(32) / 32
        hd = np.ones(16) / 16
        score = _compare_signatures(hb, hd, hb, hd)
        assert score == pytest.approx(1.0, abs=1e-6)

    def test_falls_back_to_brightness_if_one_depth_missing(self):
        hb = np.ones(32) / 32
        hd = np.ones(16) / 16
        # baseline has depth, current does not
        score = _compare_signatures(hb, hd, hb, None)
        assert score == pytest.approx(1.0, abs=1e-6)


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
            assert received and received[0]["name"] == "Bedroom"
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
        finally:
            svc.stop()
        data = json.loads((tmp_path / "room_state.json").read_text())
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
            assert svc.room_name == "Garage"
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


# ── Panoramic signature capture ───────────────────────────────────────────────


class TestPanoramicSignature:
    def test_returns_none_when_no_vision_service(self, tmp_path):
        svc = _make_service(tmp_path, vision_svc=None)
        assert svc._panoramic_signature() is None

    def test_returns_none_when_latest_frame_is_none(self, tmp_path):
        svc = _make_service(tmp_path, vision_svc=_make_vision(None))
        # no bus sweep (current_angle is None), single frame returns None
        assert svc._panoramic_signature() is None

    def test_returns_dict_with_brightness_key(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert "brightness" in sig
        assert len(sig["brightness"]) == 32

    def test_returns_mean_brightness_key(self, tmp_path):
        frame = np.full((480, 640, 3), 128, dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert "mean_brightness" in sig
        assert sig["mean_brightness"] > 100  # bright frame

    def test_dark_frame_mean_brightness_low(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert sig["mean_brightness"] < 15  # should be near 0

    def test_returns_depth_none_when_no_depth_data(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert sig["depth"] is None

    def test_handles_vision_exception_gracefully(self, tmp_path):
        mock_vis = MagicMock()
        mock_vis.latest_frame.side_effect = RuntimeError("camera gone")
        svc = _make_service(tmp_path, vision_svc=mock_vis)
        assert svc._panoramic_signature() is None

    def test_includes_depth_when_available(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        depth = np.random.uniform(0.5, 4.0, (480, 640))
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        svc._latest_depth_arr = depth
        svc._latest_depth_ts = time.monotonic()
        sig = svc._panoramic_signature()
        assert sig is not None
        assert sig["depth"] is not None
        assert len(sig["depth"]) == 16


# ── Depth map bus handlers ────────────────────────────────────────────────────


class TestDepthMapHandlers:
    def test_on_depth_map_caches_array(self, tmp_path):
        svc = _make_service(tmp_path)
        payload = {
            "depth_m": [[1.0, 2.0], [3.0, 4.0]],
            "ts": time.monotonic(),
        }
        svc._on_depth_map(None, payload)
        with svc._depth_lock:
            assert svc._latest_depth_arr is not None
            assert svc._latest_depth_arr.shape == (2, 2)

    def test_on_depth_map_ignores_non_dict(self, tmp_path):
        svc = _make_service(tmp_path)
        svc._on_depth_map(None, "bad payload")
        with svc._depth_lock:
            assert svc._latest_depth_arr is None

    def test_on_mono_depth_map_caches_normalised(self, tmp_path):
        svc = _make_service(tmp_path)
        payload = {"depth_rel": [[0.5, 0.8], [0.2, 1.0]], "scale_factor": None}
        svc._on_mono_depth_map(None, payload)
        with svc._depth_lock:
            assert svc._latest_depth_arr is not None


# ── State persistence ─────────────────────────────────────────────────────────


class TestStatePersistence:
    def test_load_state_with_brightness_sig(self, tmp_path):
        sig = [0.03125] * 32
        state_path = tmp_path / "room_state.json"
        state_path.write_text(json.dumps({"name": "Dining Room", "brightness_sig": sig}))
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._load_state()
        assert svc.room_name == "Dining Room"
        assert svc._baseline_brightness is not None
        assert len(svc._baseline_brightness) == 32

    def test_load_state_backwards_compat_signature_key(self, tmp_path):
        sig = [0.03125] * 32
        state_path = tmp_path / "room_state.json"
        state_path.write_text(json.dumps({"name": "Old Room", "signature": sig}))
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._load_state()
        assert svc._baseline_brightness is not None

    def test_load_state_handles_missing_file(self, tmp_path):
        svc = RoomService(
            bus=MessageBus(), vision_service=None, state_path=tmp_path / "nope.json"
        )
        svc._load_state()
        assert svc.room_name is None

    def test_load_state_handles_corrupt_file(self, tmp_path):
        state_path = tmp_path / "room_state.json"
        state_path.write_text("{bad json}")
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._load_state()
        assert svc.room_name is None

    def test_save_and_reload(self, tmp_path):
        state_path = tmp_path / "room_state.json"
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._room_name = "Garage"
        svc._baseline_brightness = np.ones(32) / 32
        svc._baseline_depth = np.ones(16) / 16
        svc._save_state()

        svc2 = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc2._load_state()
        assert svc2.room_name == "Garage"
        assert svc2._baseline_brightness is not None
        assert svc2._baseline_depth is not None


# ── Divergence detection ──────────────────────────────────────────────────────


class TestDivergenceDetection:
    def test_establishes_baseline_on_first_check(self, tmp_path):
        frame = np.full((480, 640, 3), 100, dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        svc.start()
        try:
            assert svc._baseline_brightness is None
            svc._check_scene()
            assert svc._baseline_brightness is not None
        finally:
            svc.stop()

    def test_similar_scene_resets_divergence_counter(self, tmp_path):
        frame = np.full((480, 640, 3), 100, dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        svc._baseline_brightness = _compute_brightness_signature(frame)
        svc._consec_diverged = 2
        svc.start()
        try:
            svc._check_scene()
            assert svc._consec_diverged == 0
        finally:
            svc.stop()

    def test_different_scene_increments_divergence_counter(self, tmp_path):
        baseline_frame = np.full((480, 640, 3), 20, dtype=np.uint8)
        current_frame = np.full((480, 640, 3), 220, dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(current_frame))
        svc._baseline_brightness = _compute_brightness_signature(baseline_frame)
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
        svc = _make_service(tmp_path, vision_svc=_make_vision(current_frame), room_name="Basement")
        spoken: list = []
        svc.bus.subscribe("av.say", lambda _t, p: spoken.append(p))
        svc._baseline_brightness = _compute_brightness_signature(baseline_frame)
        svc._consec_diverged = 2
        svc._last_prompt_ts = float("-inf")
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
        svc = _make_service(tmp_path, vision_svc=_make_vision(current_frame), room_name="Den")
        spoken: list = []
        svc.bus.subscribe("av.say", lambda _t, p: spoken.append(p))
        svc._baseline_brightness = _compute_brightness_signature(baseline_frame)
        svc._consec_diverged = 2
        svc._last_prompt_ts = time.monotonic()  # cooldown active
        svc.start()
        try:
            svc._check_scene()
            time.sleep(0.3)
            divergence_prompts = [s for s in spoken if "different" in s.get("text", "")]
            assert not divergence_prompts
        finally:
            svc.stop()

    def test_motion_position_updates_current_angle(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("motion.position", {"angle": 155.0})
            deadline = time.monotonic() + 1.0
            while svc._current_angle is None and time.monotonic() < deadline:
                time.sleep(0.05)
            assert svc._current_angle == pytest.approx(155.0)
        finally:
            svc.stop()

    def test_low_light_skips_divergence_increment(self, tmp_path):
        """Turning off the lights should not increment the divergence counter."""
        baseline_frame = np.full((480, 640, 3), 120, dtype=np.uint8)   # normal
        dark_frame = np.zeros((480, 640, 3), dtype=np.uint8)            # pitch black
        svc = _make_service(tmp_path, vision_svc=_make_vision(dark_frame))
        svc._baseline_brightness = _compute_brightness_signature(baseline_frame)
        svc._consec_diverged = 0
        svc.start()
        try:
            svc._check_scene()  # should skip — low light
            assert svc._consec_diverged == 0  # unchanged
        finally:
            svc.stop()

    def test_low_light_does_not_reset_divergence_counter(self, tmp_path):
        """Low-light samples are truly skipped — they don't reset an existing counter."""
        baseline_frame = np.full((480, 640, 3), 120, dtype=np.uint8)
        dark_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(dark_frame))
        svc._baseline_brightness = _compute_brightness_signature(baseline_frame)
        svc._consec_diverged = 2  # already diverged before lights went out
        svc.start()
        try:
            svc._check_scene()  # low light — should leave counter alone
            assert svc._consec_diverged == 2  # unchanged
        finally:
            svc.stop()
