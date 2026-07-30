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
    _DEFAULT_CONSEC_DIVERGED,
    _DEFAULT_LOW_LIGHT_THRESH,
    _DEFAULT_PROMPT_COOLDOWN_S,
    _DEFAULT_SAMPLE_INTERVAL_S,
    _DEFAULT_SIMILARITY_THRESH,
    _DEFAULT_SKIP_WHEN_FACES,
    _bhattacharyya,
    _compare_signatures,
    _compute_brightness_signature,
    _compute_depth_signature,
    _compute_gradient_embedding,
    _compute_signature,  # backwards-compat alias
    _cosine_similarity,
    _identify_room_via_claude,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_service(
    tmp_path: Path,
    vision_svc=None,
    room_name: Optional[str] = None,
    cfg: Optional[dict] = None,
    anthropic_enabled: bool = True,
) -> RoomService:
    bus = MessageBus()
    state_path = tmp_path / "room_state.json"
    if room_name is not None:
        state_path.write_text(json.dumps({"name": room_name, "brightness_sig": None}))
    svc = RoomService(
        bus=bus, vision_service=vision_svc, state_path=state_path, cfg=cfg,
        anthropic_enabled=anthropic_enabled,
    )
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


# ── Gradient embedding ────────────────────────────────────────────────────────


class TestComputeGradientEmbedding:
    def test_returns_float_array_correct_length(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        emb = _compute_gradient_embedding(frame)
        assert isinstance(emb, np.ndarray)
        assert len(emb) == 384  # 6 × 8 cells × 8 bins

    def test_is_l2_normalized(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        emb = _compute_gradient_embedding(frame)
        assert np.linalg.norm(emb) == pytest.approx(1.0, abs=1e-5)

    def test_grayscale_input(self):
        frame = np.full((480, 640), 128, dtype=np.uint8)
        emb = _compute_gradient_embedding(frame)
        assert len(emb) == 384

    def test_lighting_invariance_uniform_scale(self):
        """Uniformly scaling brightness should not change the embedding."""
        rng = np.random.default_rng(7)
        # Create a structured frame (edges, not uniform)
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, 320:, :] = 80      # right half brighter
        frame[240:, :, :] += 40     # bottom half even brighter
        frame_bright = np.clip(frame.astype(int) * 2, 0, 255).astype(np.uint8)
        e1 = _compute_gradient_embedding(frame)
        e2 = _compute_gradient_embedding(frame_bright)
        # Gradient orientations are the same; only magnitudes change
        # (which are normalised per cell) → embeddings should be very similar
        assert _cosine_similarity(e1, e2) > 0.95

    def test_different_structure_different_embedding(self):
        """Frames with fundamentally different edge structure should differ."""
        frame_h = np.zeros((480, 640, 3), dtype=np.uint8)
        frame_h[240, :, :] = 200   # single horizontal edge
        frame_v = np.zeros((480, 640, 3), dtype=np.uint8)
        frame_v[:, 320, :] = 200   # single vertical edge
        e1 = _compute_gradient_embedding(frame_h)
        e2 = _compute_gradient_embedding(frame_v)
        assert _cosine_similarity(e1, e2) < 0.95


# ── Cosine similarity ─────────────────────────────────────────────────────────


class TestCosineSimilarity:
    def test_identical_vectors_score_one(self):
        v = np.array([0.6, 0.8])
        assert _cosine_similarity(v, v) == pytest.approx(1.0, abs=1e-6)

    def test_orthogonal_vectors_score_zero(self):
        v1 = np.array([1.0, 0.0])
        v2 = np.array([0.0, 1.0])
        assert _cosine_similarity(v1, v2) == pytest.approx(0.0, abs=1e-6)


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

    def test_uses_embedding_when_both_present(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        e = _compute_gradient_embedding(frame)
        hb = np.ones(32) / 32
        score = _compare_signatures(hb, None, hb, None, e, e)
        assert score == pytest.approx(1.0, abs=1e-5)

    def test_embedding_plus_depth_when_both_available(self):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        e = _compute_gradient_embedding(frame)
        hb = np.ones(32) / 32
        hd = np.ones(16) / 16
        score = _compare_signatures(hb, hd, hb, hd, e, e)
        assert score == pytest.approx(1.0, abs=1e-5)

    def test_falls_back_to_brightness_when_no_base_embedding(self):
        """Legacy state file (no embedding) → histogram comparison."""
        hb = np.ones(32) / 32
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        e_curr = _compute_gradient_embedding(frame)
        # base_embedding=None → even if current has embedding, fall back to histograms
        score = _compare_signatures(hb, None, hb, None, None, e_curr)
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

    def test_returns_embedding_key(self, tmp_path):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert "embedding" in sig
        assert sig["embedding"] is not None
        assert len(sig["embedding"]) == 384

    def test_embedding_is_l2_normalized(self, tmp_path):
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame))
        sig = svc._panoramic_signature()
        assert sig is not None
        assert np.linalg.norm(sig["embedding"]) == pytest.approx(1.0, abs=1e-5)

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

    def test_save_and_reload_with_embedding(self, tmp_path):
        state_path = tmp_path / "room_state.json"
        frame = np.random.randint(0, 256, (480, 640, 3), dtype=np.uint8)
        emb = _compute_gradient_embedding(frame)
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._room_name = "Office"
        svc._baseline_brightness = np.ones(32) / 32
        svc._baseline_embedding = emb
        svc._save_state()

        svc2 = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc2._load_state()
        assert svc2.room_name == "Office"
        assert svc2._baseline_embedding is not None
        assert len(svc2._baseline_embedding) == 384
        assert np.allclose(svc2._baseline_embedding, emb)

    def test_load_state_without_embedding_leaves_embedding_none(self, tmp_path):
        """Legacy state files without 'embedding' key should load cleanly."""
        sig = [0.03125] * 32
        state_path = tmp_path / "room_state.json"
        state_path.write_text(json.dumps({"name": "Legacy Room", "brightness_sig": sig}))
        svc = RoomService(bus=MessageBus(), vision_service=None, state_path=state_path)
        svc._load_state()
        assert svc._baseline_embedding is None   # no embedding in legacy file


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
            assert svc._baseline_embedding is not None  # embedding also captured
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

    def test_embedding_lighting_change_does_not_diverge(self, tmp_path):
        """
        Gradient embedding approach: same room with changed brightness should
        NOT diverge — the embedding is lighting-invariant.
        """
        # Structured frame simulating a room
        base_frame = np.zeros((480, 640, 3), dtype=np.uint8)
        base_frame[:, 320:, :] = 80   # right half brighter (like a wall)
        base_frame[240:, :, :] += 40  # bottom half differently lit
        # Brighter version: same structure, 2× brightness
        bright_frame = np.clip(base_frame.astype(int) * 2, 0, 255).astype(np.uint8)

        svc = _make_service(tmp_path, vision_svc=_make_vision(bright_frame))
        svc._baseline_brightness = _compute_brightness_signature(base_frame)
        svc._baseline_embedding = _compute_gradient_embedding(base_frame)
        svc._consec_diverged = 0
        svc.start()
        try:
            svc._check_scene()
            assert svc._consec_diverged == 0  # lighting change must not trigger divergence
        finally:
            svc.stop()


# ── Config tests ──────────────────────────────────────────────────────────────


class TestRoomServiceConfig:
    """RoomService reads tunables from cfg dict; missing keys fall back to defaults."""

    def test_defaults_applied_when_no_cfg(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc._sample_interval_s == _DEFAULT_SAMPLE_INTERVAL_S
        assert svc._consec_diverged_threshold == _DEFAULT_CONSEC_DIVERGED
        assert svc._similarity_thresh == _DEFAULT_SIMILARITY_THRESH
        assert svc._prompt_cooldown_s == _DEFAULT_PROMPT_COOLDOWN_S
        assert svc._low_light_thresh == _DEFAULT_LOW_LIGHT_THRESH
        assert svc._skip_when_faces == _DEFAULT_SKIP_WHEN_FACES

    def test_cfg_overrides_sample_interval(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"sample_interval_s": 120})
        assert svc._sample_interval_s == pytest.approx(120.0)

    def test_cfg_overrides_similarity_thresh(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"similarity_thresh": 0.75})
        assert svc._similarity_thresh == pytest.approx(0.75)

    def test_cfg_overrides_consec_diverged(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"consec_diverged": 5})
        assert svc._consec_diverged_threshold == 5

    def test_cfg_overrides_prompt_cooldown(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"prompt_cooldown_s": 300})
        assert svc._prompt_cooldown_s == pytest.approx(300.0)

    def test_cfg_overrides_low_light_thresh(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"low_light_thresh": 30.0})
        assert svc._low_light_thresh == pytest.approx(30.0)

    def test_cfg_overrides_skip_when_faces(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"skip_when_faces": False})
        assert svc._skip_when_faces is False

    def test_partial_cfg_uses_defaults_for_missing_keys(self, tmp_path):
        svc = _make_service(tmp_path, cfg={"sample_interval_s": 300})
        assert svc._sample_interval_s == pytest.approx(300.0)
        assert svc._similarity_thresh == pytest.approx(_DEFAULT_SIMILARITY_THRESH)

    def test_empty_cfg_dict_uses_all_defaults(self, tmp_path):
        svc = _make_service(tmp_path, cfg={})
        assert svc._sample_interval_s == pytest.approx(_DEFAULT_SAMPLE_INTERVAL_S)


# ── Face-skip tests ───────────────────────────────────────────────────────────


class TestFaceSkip:
    """Samples are skipped when faces are visible (skip_when_faces=True)."""

    def _bright_frame(self) -> np.ndarray:
        """Return a well-lit structured frame that would normally pass the brightness check."""
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        frame[:, 320:] = 100
        frame[240:] += 30
        return frame

    def test_on_faces_updates_face_count(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("perception.faces", {"faces": [{"id": 1}, {"id": 2}]})
            deadline = time.monotonic() + 1.0
            while True:
                with svc._face_lock:
                    count = svc._face_count
                if count == 2 or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            assert count == 2
        finally:
            svc.stop()

    def test_on_faces_clears_count_when_empty(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("perception.faces", {"faces": [{"id": 1}]})
            time.sleep(0.1)
            svc.bus.publish("perception.faces", {"faces": []})
            deadline = time.monotonic() + 1.0
            while True:
                with svc._face_lock:
                    count = svc._face_count
                if count == 0 or time.monotonic() > deadline:
                    break
                time.sleep(0.02)
            assert count == 0
        finally:
            svc.stop()

    def test_check_scene_skips_when_faces_present(self, tmp_path):
        """With skip_when_faces=True, a sample is skipped when face_count > 0."""
        frame = self._bright_frame()
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame), cfg={"skip_when_faces": True})
        base_embedding = _compute_gradient_embedding(
            np.zeros((480, 640, 3), dtype=np.uint8)  # very different baseline
        )
        svc._baseline_brightness = _compute_brightness_signature(
            np.zeros((480, 640, 3), dtype=np.uint8)
        )
        svc._baseline_embedding = base_embedding
        svc._consec_diverged = 0
        with svc._face_lock:
            svc._face_count = 1  # simulate face visible
        svc.start()
        try:
            svc._check_scene()  # should skip entirely
            assert svc._consec_diverged == 0
        finally:
            svc.stop()

    def test_check_scene_does_not_skip_when_no_faces(self, tmp_path):
        """When no faces are present, the sample proceeds normally."""
        frame = self._bright_frame()
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame), cfg={"skip_when_faces": True})
        svc._baseline_brightness = _compute_brightness_signature(frame)
        svc._baseline_embedding = _compute_gradient_embedding(frame)
        svc._consec_diverged = 0
        with svc._face_lock:
            svc._face_count = 0  # no faces
        svc.start()
        try:
            svc._check_scene()  # should proceed and find similar scene (same frame)
            assert svc._consec_diverged == 0  # same scene → no divergence
        finally:
            svc.stop()

    def test_face_skip_preserves_existing_counter(self, tmp_path):
        """Face-skip does not reset an existing divergence counter."""
        frame = self._bright_frame()
        svc = _make_service(tmp_path, vision_svc=_make_vision(frame), cfg={"skip_when_faces": True})
        svc._baseline_brightness = _compute_brightness_signature(frame)
        svc._consec_diverged = 2
        with svc._face_lock:
            svc._face_count = 1  # face visible — skip
        svc.start()
        try:
            svc._check_scene()
            assert svc._consec_diverged == 2  # unchanged
        finally:
            svc.stop()

    def test_skip_when_faces_false_does_not_skip(self, tmp_path):
        """When skip_when_faces=False, faces do not suppress the sample."""
        # Use a very different frame vs baseline so divergence would fire normally
        baseline = np.zeros((480, 640, 3), dtype=np.uint8)
        current = np.full((480, 640, 3), 200, dtype=np.uint8)
        svc = _make_service(
            tmp_path,
            vision_svc=_make_vision(current),
            cfg={"skip_when_faces": False},
        )
        svc._baseline_brightness = _compute_brightness_signature(baseline)
        svc._baseline_embedding = _compute_gradient_embedding(baseline)
        svc._consec_diverged = 0
        with svc._face_lock:
            svc._face_count = 3  # faces visible, but skip_when_faces=False
        svc.start()
        try:
            svc._check_scene()
            # sample was NOT skipped — divergence counter should have moved
            assert svc._consec_diverged > 0
        finally:
            svc.stop()


# ── Anthropic API toggle ──────────────────────────────────────────────────────


class TestIdentifyRoomViaClaude:
    """_identify_room_via_claude() respects the enabled flag and API key gating."""

    def test_disabled_returns_none_without_reaching_api_key_check(self, monkeypatch):
        """enabled=False short-circuits before even checking ANTHROPIC_API_KEY."""
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        # Set a bogus key to prove the short-circuit happens before key inspection
        # would otherwise proceed further into the function.
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-should-not-be-used")
        result = _identify_room_via_claude(frame, enabled=False)
        assert result is None

    def test_enabled_but_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        result = _identify_room_via_claude(frame, enabled=True)
        assert result is None

    def test_default_enabled_true_when_omitted(self, monkeypatch):
        """Calling without the enabled kwarg preserves the pre-toggle default behavior."""
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        frame = np.zeros((64, 64, 3), dtype=np.uint8)
        # No enabled kwarg passed — should behave as enabled=True (short-circuits on
        # missing API key, but does NOT short-circuit on the disabled-flag branch).
        result = _identify_room_via_claude(frame)
        assert result is None


class TestRoomServiceAnthropicToggle:
    """RoomService wires anthropic_enabled through to _identify_room_via_claude."""

    def test_defaults_to_enabled(self, tmp_path):
        svc = _make_service(tmp_path)
        assert svc._anthropic_enabled is True

    def test_constructor_can_disable(self, tmp_path):
        svc = _make_service(tmp_path, anthropic_enabled=False)
        assert svc._anthropic_enabled is False

    def test_bus_event_disables_at_runtime(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("anthropic.set_enabled", {"enabled": False})
            assert svc._anthropic_enabled is False
        finally:
            svc.stop()

    def test_bus_event_re_enables_at_runtime(self, tmp_path):
        svc = _make_service(tmp_path, anthropic_enabled=False)
        svc.start()
        try:
            svc.bus.publish("anthropic.set_enabled", {"enabled": True})
            assert svc._anthropic_enabled is True
        finally:
            svc.stop()

    def test_bus_event_ignores_non_dict_payload(self, tmp_path):
        svc = _make_service(tmp_path)
        svc.start()
        try:
            svc.bus.publish("anthropic.set_enabled", "not-a-dict")
            assert svc._anthropic_enabled is True
        finally:
            svc.stop()

    def test_status_dict_reports_anthropic_enabled(self, tmp_path):
        svc = _make_service(tmp_path, anthropic_enabled=False)
        status = svc.get_status_dict()
        assert status["anthropic_enabled"] is False

    def test_check_scene_does_not_call_claude_when_disabled(self, tmp_path):
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        svc = _make_service(
            tmp_path, vision_svc=_make_vision(frame), anthropic_enabled=False,
        )
        with patch(
            "src.services.room_service._identify_room_via_claude"
        ) as mock_claude:
            svc.start()
            try:
                svc._check_scene()
            finally:
                svc.stop()
            # Whenever it is invoked, it must be told the feature is disabled.
            for call in mock_claude.call_args_list:
                _, kwargs = call
                assert kwargs.get("enabled") is False
