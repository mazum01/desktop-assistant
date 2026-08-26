"""
Tests for ObjectService and scene description builder (sim mode).
"""
import time
import pytest

from src.services.object_service import ObjectService, ObjectConfig, _build_scene_description
from src.core.bus import MessageBus


# ── _build_scene_description ────────────────────────────────────────────────

def test_describe_empty():
    text = _build_scene_description(None, None)
    assert "don't see" in text.lower()


def test_describe_faces_only():
    faces = {"faces": [{"name": "Alice"}, {"name": "Bob"}]}
    text = _build_scene_description(faces, None)
    assert "Alice" in text
    assert "Bob" in text
    assert "see" in text.lower()


def test_describe_objects_only():
    objs = {"objects": [
        {"label": "laptop", "confidence": 0.9},
        {"label": "cup",    "confidence": 0.8},
    ]}
    text = _build_scene_description(None, objs)
    assert "laptop" in text
    assert "cup" in text


def test_describe_faces_and_objects():
    faces = {"faces": [{"name": "Charlie"}]}
    objs  = {"objects": [{"label": "keyboard", "confidence": 0.75}]}
    text = _build_scene_description(faces, objs)
    assert "Charlie" in text
    assert "keyboard" in text


def test_describe_guests_excluded():
    faces = {"faces": [{"name": "Guest 1"}, {"name": "Alice"}]}
    text = _build_scene_description(faces, None)
    assert "Guest" not in text
    assert "Alice" in text


def test_describe_duplicate_objects_counted():
    objs = {"objects": [
        {"label": "chair", "confidence": 0.8},
        {"label": "chair", "confidence": 0.75},
        {"label": "chair", "confidence": 0.7},
    ]}
    text = _build_scene_description(None, objs)
    assert "3" in text or "three" in text.lower() or "chair" in text


# ── ObjectService lifecycle ─────────────────────────────────────────────────

class _DummyVision:
    hardware_ready = False

    def latest_frame(self):
        import numpy as np
        return np.zeros((480, 640, 3), dtype=np.uint8)


def test_object_service_start_stop():
    bus = MessageBus()
    vis = _DummyVision()
    svc = ObjectService(bus=bus, vision_service=vis, config=ObjectConfig(max_fps=1.0))
    svc.start()
    time.sleep(0.1)
    assert svc.is_running()
    svc.stop()
    assert not svc.is_running()


def test_object_service_describe_event():
    """vision.describe publishes av.say when faces/objects are in the bus."""
    bus = MessageBus()
    vis = _DummyVision()
    svc = ObjectService(bus=bus, vision_service=vis)
    svc.start()

    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p))

    bus.publish("vision.describe", {})
    time.sleep(0.2)
    svc.stop()

    assert len(spoken) == 1
    assert "text" in spoken[0]
    assert len(spoken[0]["text"]) > 0


def test_object_service_find_object_query():
    bus = MessageBus()
    vis = _DummyVision()
    svc = ObjectService(bus=bus, vision_service=vis)
    svc.start()

    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p))
    bus.publish("perception.objects", {"objects": [
        {"label": "cup", "confidence": 0.91, "bbox": [10, 10, 40, 40]},
        {"label": "laptop", "confidence": 0.82, "bbox": [50, 50, 100, 100]},
    ]})

    result = svc.query_objects("mug", speak=True)
    svc.stop()

    assert result["ok"] is True
    assert result["results"][0]["label"] == "cup"
    assert spoken
    assert "cup" in spoken[0]["text"]
