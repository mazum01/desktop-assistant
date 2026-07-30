"""Tests for FaceService — greeting logic, cooldown, absence detection, phrases."""
import json
import subprocess
import time
import pytest

from unittest.mock import MagicMock, call

from src.core.bus import MessageBus
from src.services.face_service import (
    FaceService,
    _jittered_cooldown,
    _time_bucket,
    _GREET_MORNING,
    _GREET_AFTERNOON,
    _GREET_EVENING,
    _GREET_NIGHT,
    _NEW_FACE_PHRASES,
    _ABSENCE_DEBOUNCE_FRAMES,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_face(face_id="f1", name="Guest 1", is_new=True, score=0.0, confidence=0.9):
    return {
        "bbox": [0, 0, 100, 100],
        "centroid": [50, 50],
        "confidence": confidence,
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


def _wait():
    import threading
    threading.Event().wait(0.05)


@pytest.fixture
def bus():
    return MessageBus()


@pytest.fixture
def svc(bus):
    reg = _mock_registry()
    service = FaceService(
        bus=bus,
        registry=reg,
        greeting_cooldown_min=0.5,   # 30 s for test purposes
        greeting_cooldown_jitter_pct=25.0,
        min_absence_s=5.0,
        confidence_threshold=0.5,
        guest_intro_delay_min=0.0,   # instant for tests — cooldown tested separately
    )
    service.start()
    yield service
    service.stop()


# ── Unit: jitter helper ───────────────────────────────────────────────────────

def test_jittered_cooldown_within_range():
    base_min = 30.0
    jitter = 25.0
    low  = base_min * (1 - jitter / 100) * 60
    high = base_min * (1 + jitter / 100) * 60
    for _ in range(100):
        val = _jittered_cooldown(base_min, jitter)
        assert low <= val <= high, f"jitter out of range: {val}"


def test_jittered_cooldown_zero_jitter():
    val = _jittered_cooldown(30.0, 0.0)
    assert val == pytest.approx(1800.0, abs=0.1)


# ── Unit: time bucket ─────────────────────────────────────────────────────────

def test_time_bucket_returns_valid_bucket():
    bucket = _time_bucket()
    assert bucket in ("morning", "afternoon", "evening", "night")


# ── New face greeting ────────────────────────────────────────────────────────

def test_new_face_triggers_av_say(bus, svc):
    """A brand-new face should cause an av.say event (once per session)."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    bus.publish("perception.faces", _faces_payload(_make_face(is_new=True)))
    _wait()

    assert any(spoken), "expected av.say but got nothing"
    assert any(phrase in spoken[0] for phrase in ["Desktop Assistant", "VERA", "new face", "hello", "Hello"])


def test_new_face_only_greeted_once_per_session(bus, svc):
    """New face intro fires exactly once per session (session guard)."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    for _ in range(3):
        bus.publish("perception.faces", _faces_payload(_make_face(face_id="f1", is_new=True)))
        _wait()

    # Only one greeting for new face
    assert len([s for s in spoken if any(p in s for p in ["Desktop Assistant", "VERA", "new face"])]) == 1


# ── Known face re-greet ──────────────────────────────────────────────────────

def test_known_face_greeted_when_needs_greeting(bus, svc):
    """A known face that needs greeting should trigger a spoken greeting."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.needs_greeting.return_value = True
    face = _make_face(face_id="f2", name="Alice", is_new=False, score=0.7)
    bus.publish("perception.faces", _faces_payload(face))
    _wait()

    assert any("Alice" in s for s in spoken), f"expected 'Alice' in greeting, got: {spoken}"


def test_known_face_not_greeted_within_cooldown(bus, svc):
    """A known face that does NOT need greeting should be skipped."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.needs_greeting.return_value = False
    face = _make_face(face_id="f3", name="Bob", is_new=False, score=0.7)
    bus.publish("perception.faces", _faces_payload(face))
    _wait()

    assert not any("Bob" in s for s in spoken), "should not re-greet so soon"


def test_known_face_uses_openclaw_generated_greeting(bus):
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    reg = _mock_registry()
    reg.needs_greeting.return_value = True
    service = FaceService(
        bus=bus,
        registry=reg,
        guest_intro_delay_min=0.0,
        openclaw_greetings_enabled=True,
        openclaw_greeting_timeout_s=1.0,
        openclaw_cli_path="openclaw",
    )
    service.start()

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {"outputs": [{"text": "Good to see you again, Alice. Hope your day is going well."}]}
            ),
            stderr="",
        )

    from unittest.mock import patch
    with patch("subprocess.run", side_effect=_fake_run):
        face = _make_face(face_id="f2", name="Alice", is_new=False, score=0.7)
        bus.publish("perception.faces", _faces_payload(face))
        _wait()

    service.stop()
    assert any("Alice" in s and "Good to see you again" in s for s in spoken), spoken


def test_openclaw_timeout_falls_back_to_static_phrase(bus):
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    reg = _mock_registry()
    reg.needs_greeting.return_value = True
    service = FaceService(
        bus=bus,
        registry=reg,
        guest_intro_delay_min=0.0,
        openclaw_greetings_enabled=True,
        openclaw_greeting_timeout_s=0.5,
        openclaw_cli_path="openclaw",
    )
    service.start()

    from unittest.mock import patch
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="openclaw", timeout=0.5)):
        face = _make_face(face_id="f3", name="Bob", is_new=False, score=0.7)
        bus.publish("perception.faces", _faces_payload(face))
        _wait()

    service.stop()
    assert any("Bob" in s for s in spoken), spoken


def test_openclaw_prepends_cli_dir_to_path_for_daemon_env(bus):
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    reg = _mock_registry()
    reg.needs_greeting.return_value = True
    service = FaceService(
        bus=bus,
        registry=reg,
        guest_intro_delay_min=0.0,
        openclaw_greetings_enabled=True,
        openclaw_greeting_timeout_s=1.0,
        openclaw_cli_path="/home/starter/.nvm/versions/node/v24.15.0/bin/openclaw",
    )

    from unittest.mock import patch
    with patch.object(service, "_resolve_openclaw_cli_path", return_value="/home/starter/.nvm/versions/node/v24.15.0/bin/openclaw"):
        service.start()

    def _fake_run(*args, **kwargs):
        path = kwargs.get("env", {}).get("PATH", "")
        assert "/home/starter/.nvm/versions/node/v24.15.0/bin" in path.split(":")
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"outputs": [{"text": "Great to see you again, Alice!"}]}),
            stderr="",
        )

    with patch("subprocess.run", side_effect=_fake_run):
        face = _make_face(face_id="f8", name="Alice", is_new=False, score=0.7)
        bus.publish("perception.faces", _faces_payload(face))
        _wait()

    service.stop()
    assert any("Alice" in s for s in spoken), spoken


# ── Absence detection + debounce ─────────────────────────────────────────────

def test_absence_debounce_requires_n_frames(bus, svc):
    """mark_absent should not be called until ABSENCE_DEBOUNCE_FRAMES consecutive absent frames."""
    face = _make_face(face_id="f5", name="Dave", is_new=False, score=0.8)

    # First frame: face present
    bus.publish("perception.faces", _faces_payload(face))
    _wait()

    reg = svc._registry
    reg.mark_absent.reset_mock()

    # Send (debounce - 1) empty frames — should NOT call mark_absent yet
    for _ in range(_ABSENCE_DEBOUNCE_FRAMES - 1):
        bus.publish("perception.faces", _faces_payload())
        _wait()
    assert not reg.mark_absent.called, "should not mark absent before debounce threshold"

    # One more empty frame — now it should mark absent
    bus.publish("perception.faces", _faces_payload())
    _wait()
    reg.mark_absent.assert_called_once()


def test_absence_counter_resets_on_reappearance(bus, svc):
    """If a face reappears before debounce threshold, counter resets."""
    face = _make_face(face_id="f6", name="Eve", is_new=False, score=0.8)

    bus.publish("perception.faces", _faces_payload(face))
    _wait()
    svc._registry.mark_absent.reset_mock()

    # 1 empty frame (below threshold)
    bus.publish("perception.faces", _faces_payload())
    _wait()
    # Face reappears
    bus.publish("perception.faces", _faces_payload(face))
    _wait()
    # Counter should have reset, no absent mark
    assert not svc._registry.mark_absent.called


# ── Low-confidence filter ────────────────────────────────────────────────────

def test_low_confidence_face_not_greeted(bus, svc):
    """Faces below confidence_threshold should not trigger greetings."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.needs_greeting.return_value = True
    face = _make_face(face_id="f7", name="Alice", is_new=False, score=0.7, confidence=0.1)
    bus.publish("perception.faces", _faces_payload(face))
    _wait()

    assert not any("Alice" in s for s in spoken), "low-confidence face should not be greeted"


# ── Name assignment via face.meet ────────────────────────────────────────────

def test_meet_command_triggers_confirmation(bus, svc):
    """face.meet event should speak a confirmation with the provided name."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    svc._registry.get_current_face_id.return_value = "f4"
    bus.publish("face.meet", {"name": "Charlie"})
    _wait()

    assert any("Charlie" in s for s in spoken), f"expected name in confirmation, got: {spoken}"


def test_meet_with_explicit_face_id_does_not_rename_current_face(bus, svc):
    """Web-UI rename (face_id in payload) must not re-name the current face.

    Bug scenario: web UI renames face X but Mark is in frame.  Without the fix,
    _on_meet would call get_current_face_id() and rename Mark instead.
    """
    renamed_ids = []
    svc._registry.set_name.side_effect = lambda fid, name: renamed_ids.append(fid)
    svc._registry.get_face.return_value = {"name": "OldName"}

    # Mark is in the camera right now
    svc._registry.get_current_face_id.return_value = "mark-face-id"

    # Web UI renames a *different* face explicitly
    bus.publish("face.meet", {"name": "Alice", "face_id": "target-face-id"})
    _wait()

    # Only the explicit face_id should have been renamed — NOT mark-face-id
    assert renamed_ids == ["target-face-id"], (
        f"Expected only target-face-id renamed, got: {renamed_ids}"
    )
    svc._registry.get_current_face_id.assert_not_called()


def test_meet_without_face_id_falls_back_to_current_face(bus, svc):
    """CLI meet (no face_id) should still use get_current_face_id()."""
    svc._registry.get_current_face_id.return_value = "cli-face-id"
    svc._registry.get_face.return_value = {"name": "Old"}

    bus.publish("face.meet", {"name": "Bob"})
    _wait()

    svc._registry.get_current_face_id.assert_called()


# ── Varied phrases ───────────────────────────────────────────────────────────

def test_varied_phrases_no_immediate_repeat(svc):
    """_pick_phrase should not return the same phrase twice in a row."""
    phrases = []
    for _ in range(30):
        p = svc._pick_phrase("Alice")
        phrases.append(p)
    for i in range(len(phrases) - 1):
        assert phrases[i] != phrases[i + 1], f"repeated phrase at index {i}: {phrases[i]}"


def test_pick_phrase_contains_name(svc):
    """Every phrase returned by _pick_phrase should contain the person's name."""
    for _ in range(20):
        phrase = svc._pick_phrase("TestUser")
        assert "TestUser" in phrase, f"Name not in phrase: {phrase}"


def test_new_face_phrases_are_distinct():
    """All new face intro phrases are unique."""
    assert len(_NEW_FACE_PHRASES) == len(set(_NEW_FACE_PHRASES))


# ── Greeting cooldown update via bus ─────────────────────────────────────────

def test_set_greeting_cooldown_via_bus(bus, svc):
    """Sending tracking.set_greeting_cooldown updates _cooldown_min."""
    bus.publish("tracking.set_greeting_cooldown", {"cooldown_min": 60.0})
    _wait()
    assert svc._cooldown_min == pytest.approx(60.0)


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
    bus.publish("perception.faces", _faces_payload(face))
    _wait()


# ── Guest intro delay ────────────────────────────────────────────────────────

def test_guest_intro_delay_suppresses_immediate_greeting(bus):
    """With a non-zero delay, a new face seen once should NOT trigger greeting."""
    reg = _mock_registry()
    service = FaceService(
        bus=bus, registry=reg,
        guest_intro_delay_min=1.0 / 60.0,  # 1 second delay
    )
    service.start()
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    bus.publish("perception.faces", _faces_payload(_make_face(face_id="g1", is_new=True)))
    _wait()

    service.stop()
    assert not spoken, f"Expected no greeting before delay, got: {spoken}"


def test_guest_intro_delay_fires_after_elapsed(bus):
    """After the delay elapses, the next frame with is_new=True fires the greeting."""
    reg = _mock_registry()
    service = FaceService(
        bus=bus, registry=reg,
        guest_intro_delay_min=1.0 / 60.0,  # 1 second delay
    )
    service.start()
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    # Force a first-seen timestamp far in the past (5 s ago, past the 1s delay)
    service._guest_first_seen["g2"] = time.monotonic() - 5.0

    bus.publish("perception.faces", _faces_payload(_make_face(face_id="g2", is_new=True)))
    _wait()

    service.stop()
    assert any(spoken), "Expected greeting after delay elapsed"


def test_guest_intro_timer_resets_on_absence(bus):
    """If a guest disappears before delay, timer is cleared."""
    reg = _mock_registry()
    service = FaceService(
        bus=bus, registry=reg,
        guest_intro_delay_min=60.0,  # very long delay
    )
    service.start()

    # Simulate face appearing — starts timer
    bus.publish("perception.faces", _faces_payload(_make_face(face_id="g3", is_new=True)))
    _wait()
    assert "g3" in service._guest_first_seen

    # Face disappears
    bus.publish("perception.faces", {"faces": []})
    _wait()
    time.sleep(0.05)  # let absence handler process

    assert "g3" not in service._guest_first_seen, "Timer should have been cleared on absence"
    service.stop()


def test_guest_timer_cleared_when_face_recognized(bus):
    """If a Guest face is later recognized (is_new becomes False), timer is cleared."""
    reg = _mock_registry()
    service = FaceService(
        bus=bus, registry=reg,
        guest_intro_delay_min=60.0,  # very long delay
    )
    service.start()
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    # First: unrecognized
    bus.publish("perception.faces", _faces_payload(_make_face(face_id="g4", name="Guest 4", is_new=True)))
    _wait()
    assert "g4" in service._guest_first_seen

    # Later: recognized as known person
    known = _make_face(face_id="g4", name="Alice", is_new=False, score=0.9)
    reg.needs_greeting.return_value = False
    bus.publish("perception.faces", _faces_payload(known))
    _wait()

    assert "g4" not in service._guest_first_seen, "Timer should be cleared when face recognized"
    service.stop()


# ── Anthropic API toggle ──────────────────────────────────────────────────────


def test_uses_anthropic_model_true_by_default(bus):
    """Default openclaw_greeting_model is Anthropic/Claude-branded."""
    service = FaceService(bus=bus, registry=_mock_registry())
    assert service._uses_anthropic_model() is True


def test_uses_anthropic_model_false_for_other_model(bus):
    service = FaceService(
        bus=bus, registry=_mock_registry(),
        openclaw_greeting_model="openai/gpt-5",
    )
    assert service._uses_anthropic_model() is False


def test_anthropic_enabled_defaults_true(bus):
    service = FaceService(bus=bus, registry=_mock_registry())
    assert service._anthropic_enabled is True


def test_anthropic_enabled_constructor_override(bus):
    service = FaceService(bus=bus, registry=_mock_registry(), anthropic_enabled=False)
    assert service._anthropic_enabled is False


def test_anthropic_bus_event_toggles_at_runtime(bus):
    reg = _mock_registry()
    service = FaceService(bus=bus, registry=reg)
    service.start()
    try:
        bus.publish("anthropic.set_enabled", {"enabled": False})
        assert service._anthropic_enabled is False
        bus.publish("anthropic.set_enabled", {"enabled": True})
        assert service._anthropic_enabled is True
    finally:
        service.stop()


def test_anthropic_bus_event_ignores_non_dict_payload(bus):
    service = FaceService(bus=bus, registry=_mock_registry())
    service.start()
    try:
        bus.publish("anthropic.set_enabled", "not-a-dict")
        assert service._anthropic_enabled is True
    finally:
        service.stop()


def test_openclaw_greeting_skipped_when_anthropic_disabled_and_model_is_anthropic(bus):
    """Default model is Anthropic-branded, so disabling the switch should skip OpenClaw."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    reg = _mock_registry()
    reg.needs_greeting.return_value = True
    service = FaceService(
        bus=bus,
        registry=reg,
        guest_intro_delay_min=0.0,
        openclaw_greetings_enabled=True,
        openclaw_greeting_timeout_s=1.0,
        openclaw_cli_path="openclaw",
        anthropic_enabled=False,
    )
    service.start()

    from unittest.mock import patch
    with patch("subprocess.run") as mock_run:
        face = _make_face(face_id="fa1", name="Alice", is_new=False, score=0.7)
        bus.publish("perception.faces", _faces_payload(face))
        _wait()

    service.stop()
    # subprocess.run must never be invoked — the Anthropic-branded model is blocked.
    mock_run.assert_not_called()
    # Falls back to a static greeting phrase containing the name.
    assert any("Alice" in s for s in spoken), spoken


def test_openclaw_greeting_still_used_when_anthropic_disabled_but_model_is_not(bus):
    """A non-Anthropic OpenClaw model should be unaffected by the switch."""
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p.get("text", "")))

    reg = _mock_registry()
    reg.needs_greeting.return_value = True
    service = FaceService(
        bus=bus,
        registry=reg,
        guest_intro_delay_min=0.0,
        openclaw_greetings_enabled=True,
        openclaw_greeting_timeout_s=1.0,
        openclaw_cli_path="openclaw",
        openclaw_greeting_model="openai/gpt-5",
        anthropic_enabled=False,
    )
    service.start()

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps(
                {"outputs": [{"text": "Welcome back, Alice, hope you're well today."}]}
            ),
            stderr="",
        )

    from unittest.mock import patch
    with patch("subprocess.run", side_effect=_fake_run):
        face = _make_face(face_id="fa2", name="Alice", is_new=False, score=0.7)
        bus.publish("perception.faces", _faces_payload(face))
        _wait()

    service.stop()
    assert any("Welcome back, Alice" in s for s in spoken), spoken
