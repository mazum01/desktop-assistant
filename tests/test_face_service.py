"""Tests for FaceService — greeting logic, cooldown, name assignment."""
import time
import pytest

from unittest.mock import MagicMock, patch

from src.core.bus import MessageBus
from src.services.face_service import FaceService


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_face(face_id="f1", name="Guest 1", is_new=True, score=0.0):
    return {
        "bbox": [0, 0, 100, 100],
        "centroid": [50, 50],
        "confidence": 0.9,
        "landmarks": None,
        "face_id": face_id,
        "name": name,
        "is_new": is_new,
        "match_score": score,
    }


def _faces_payload(*faces):
    return {"count": len(faces), "faces": list(faces), "backend": "sim", "ts": time.time()}


def _mock_registry():
    """Return a mock FaceRegistry with sensible defaults."""
    r = MagicMock()
    r.get_current_face_id.return_value = "f1"
    r.needs_greeting.return_value = True
    return r


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def svc(bus):
    reg = _mock_registry()
    service = FaceService(bus=bus, registry=reg, greeting_cooldown_s=300)
    service.start()
    yield service
    service.stop()


# ── New face greeting ────────────────────────────────────────────────────────

def test_new_face_triggers_av_say(bus, svc):
    """A brand-new face should cause an av.say event."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    bus.publish("perception.faces", _faces_payload(_make_face(is_new=True)))
    import threading; threading.Event().wait(0.05)

    assert any(spoken), "expected av.say but got nothing"
    assert "Nice to meet you" in spoken[0] or "Desktop Assistant" in spoken[0]


# ── Known face re-greet ──────────────────────────────────────────────────────

def test_known_face_greeted_when_needs_greeting(bus, svc):
    """A known face that needs greeting should trigger a spoken greeting."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.needs_greeting.return_value = True
    face = _make_face(face_id="f2", name="Alice", is_new=False, score=0.7)
    bus.publish("perception.faces", _faces_payload(face))
    import threading; threading.Event().wait(0.05)

    assert any("Alice" in s for s in spoken), f"expected 'Alice' in greeting, got: {spoken}"


def test_known_face_not_greeted_within_cooldown(bus, svc):
    """A known face that does NOT need greeting should be skipped."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.needs_greeting.return_value = False
    face = _make_face(face_id="f3", name="Bob", is_new=False, score=0.7)
    bus.publish("perception.faces", _faces_payload(face))
    import threading; threading.Event().wait(0.05)

    assert not any("Bob" in s for s in spoken), "should not re-greet so soon"


# ── Name assignment via face.meet ────────────────────────────────────────────

def test_meet_command_triggers_confirmation(bus, svc):
    """face.meet event should speak a confirmation with the provided name."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.get_current_face_id.return_value = "f4"
    bus.publish("face.meet", {"name": "Charlie"})
    import threading; threading.Event().wait(0.05)

    assert any("Charlie" in s for s in spoken), f"expected name in confirmation, got: {spoken}"


# ── No crash on missing face_id ──────────────────────────────────────────────

def test_faces_without_face_id_do_not_crash(bus, svc):
    """Faces without recognition data should be silently skipped."""
    face = {
        "bbox": [0, 0, 100, 100],
        "centroid": [50, 50],
        "confidence": 0.9,
        "landmarks": None,
        "face_id": None,
        "name": None,
        "is_new": False,
        "match_score": 0.0,
    }
    # Should not raise
    bus.publish("perception.faces", _faces_payload(face))
    import threading; threading.Event().wait(0.05)


# ── Varied phrases ───────────────────────────────────────────────────────────

def test_varied_phrases_no_immediate_repeat(svc):
    """_pick_phrase should not return the same phrase twice in a row."""
    phrases = []
    for _ in range(30):
        p = svc._pick_phrase("Alice")
        phrases.append(p)
    for i in range(len(phrases) - 1):
        assert phrases[i] != phrases[i + 1], f"repeated phrase at index {i}: {phrases[i]}"

