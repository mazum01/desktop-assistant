"""Tests for WebService REST endpoints using FastAPI TestClient."""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from src.services.web_service import WebService
import src.services.web_service as web_service
from src.core.bus import MessageBus
from src.core.quiet_hours import QuietHours


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _mock_registry(thumb_exists=False):
    r = MagicMock()
    r.list_faces.return_value = [
        {
            "id": "aaaa-1111",
            "name": "Alice",
            "first_seen": time.time() - 3600,
            "last_seen": time.time() - 60,
            "last_greeted": 0,
            "seen_count": 5,
        }
    ]
    r.set_name.return_value = True
    r.delete_face.return_value = True
    r.delete_all_faces.return_value = 1
    r.thumbnail_path.return_value = Path("/tmp/fake.jpg") if thumb_exists else None
    return r


@pytest.fixture
def app_client():
    bus = MessageBus()
    svc = WebService(bus=bus, port=18080, registry=_mock_registry())
    app = svc._build_app()
    return TestClient(app), bus, svc


@pytest.fixture
def app_client_with_quiet(tmp_path):
    bus = MessageBus()
    qh = QuietHours(enabled=False, start="21:00", end="06:00")
    svc = WebService(bus=bus, port=18080, registry=_mock_registry(), quiet_hours=qh)
    app = svc._build_app()
    return TestClient(app), bus, svc, qh


# ── Faces API ────────────────────────────────────────────────────────────────

def test_get_faces_returns_list(app_client):
    client, bus, svc = app_client
    r = client.get("/api/faces")
    assert r.status_code == 200
    data = r.json()
    assert "faces" in data
    assert len(data["faces"]) == 1
    assert data["faces"][0]["name"] == "Alice"


def test_get_faces_includes_has_thumb(app_client):
    client, bus, svc = app_client
    # thumbnail_path returns None by default → has_thumb = False
    r = client.get("/api/faces")
    assert r.status_code == 200
    assert "has_thumb" in r.json()["faces"][0]
    assert r.json()["faces"][0]["has_thumb"] is False


def test_rename_face_ok(app_client):
    client, bus, svc = app_client
    r = client.put("/api/faces/aaaa-1111", json={"name": "Bob"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    svc._registry.set_name.assert_called_once_with("aaaa-1111", "Bob")


def test_rename_face_not_found(app_client):
    client, bus, svc = app_client
    svc._registry.set_name.return_value = False
    r = client.put("/api/faces/bad-id", json={"name": "Nobody"})
    assert r.status_code == 404


def test_delete_face_ok(app_client):
    client, bus, svc = app_client
    r = client.delete("/api/faces/aaaa-1111")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    svc._registry.delete_face.assert_called_once_with("aaaa-1111")


def test_delete_face_not_found(app_client):
    client, bus, svc = app_client
    svc._registry.delete_face.return_value = False
    r = client.delete("/api/faces/bad-id")
    assert r.status_code == 404


def test_delete_all_faces(app_client):
    client, bus, svc = app_client
    r = client.delete("/api/faces")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["deleted"] == 1
    svc._registry.delete_all_faces.assert_called_once()


def test_face_thumb_not_found(app_client):
    client, bus, svc = app_client
    # thumbnail_path returns None → 404
    r = client.get("/api/faces/aaaa-1111/thumb")
    assert r.status_code == 404


def test_face_thumb_found(tmp_path):
    """Thumbnail endpoint returns JPEG when file exists."""
    bus = MessageBus()
    fake_thumb = tmp_path / "aaaa-1111.jpg"
    fake_thumb.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 16)  # minimal JPEG header
    reg = _mock_registry(thumb_exists=True)
    reg.thumbnail_path.return_value = fake_thumb
    svc = WebService(bus=bus, port=18080, registry=reg)
    client = TestClient(svc._build_app())
    r = client.get("/api/faces/aaaa-1111/thumb")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")


# ── Quiet-hours API ───────────────────────────────────────────────────────────

def test_get_quiet_hours(app_client_with_quiet):
    client, bus, svc, qh = app_client_with_quiet
    r = client.get("/api/settings/quiet-hours")
    assert r.status_code == 200
    data = r.json()
    assert "enabled" in data
    assert "start" in data
    assert "end" in data


def test_get_quiet_hours_no_config(app_client):
    client, bus, svc = app_client
    # No quiet_hours configured → returns defaults
    r = client.get("/api/settings/quiet-hours")
    assert r.status_code == 200
    data = r.json()
    assert data["enabled"] is False


def test_put_quiet_hours(app_client_with_quiet):
    client, bus, svc, qh = app_client_with_quiet
    r = client.put("/api/settings/quiet-hours", json={"enabled": True, "start": "22:00", "end": "07:00"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert qh.enabled


def test_put_quiet_hours_invalid_time(app_client_with_quiet):
    client, bus, svc, qh = app_client_with_quiet
    r = client.put("/api/settings/quiet-hours", json={"enabled": True, "start": "99:99", "end": "07:00"})
    assert r.status_code == 400


# ── Controls API ──────────────────────────────────────────────────────────────

def test_say_publishes_to_bus(app_client):
    client, bus, svc = app_client
    spoken = []
    bus.subscribe("av.say", lambda t, p: spoken.append(p))
    r = client.post("/api/say", json={"text": "hello world"})
    assert r.status_code == 200
    assert any(s.get("text") == "hello world" for s in spoken)


def test_pan_publishes_to_bus(app_client):
    client, bus, svc = app_client
    pans = []
    bus.subscribe("motion.pan_to", lambda t, p: pans.append(p))
    r = client.post("/api/pan", json={"angle": 90.0})
    assert r.status_code == 200
    assert any(abs(p.get("angle", 0) - 90.0) < 0.01 for p in pans)


def test_version_publishes_to_bus(app_client):
    client, bus, svc = app_client
    events = []
    bus.subscribe("av.announce_version", lambda t, p: events.append(True))
    r = client.post("/api/version")
    assert r.status_code == 200
    assert events


def test_vision_describe_returns_text_and_speaks(app_client):
    client, bus, svc = app_client
    events = []
    bus.subscribe("av.say", lambda t, p: events.append(p))
    bus.publish("perception.faces", {"faces": [{"name": "Alice"}]})
    bus.publish("perception.objects", {"objects": [{"label": "laptop"}]})

    r = client.post("/api/vision/describe")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "description" in data
    assert "Alice" in data["description"]
    assert "laptop" in data["description"]
    assert events
    assert events[0]["text"] == data["description"]


def test_audio_record_endpoint_calls_av_service(app_client):
    client, bus, svc = app_client

    class _FakeAV:
        name = "av"

        def record_clip(self, seconds=5.0, path=None):
            return {"ok": True, "seconds": float(seconds), "path": path or "/tmp/test.wav"}

        def play_recording(self, path=None):
            return {"ok": True, "seconds": 1.0, "path": path or "/tmp/test.wav"}

    svc._all_services = [_FakeAV()]
    r = client.post("/api/audio/record", json={"seconds": 2.5, "path": "/tmp/in.wav"})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["seconds"] == pytest.approx(2.5)
    assert data["path"] == "/tmp/in.wav"


def test_audio_playback_endpoint_calls_av_service(app_client):
    client, bus, svc = app_client

    class _FakeAV:
        name = "av"

        def record_clip(self, seconds=5.0, path=None):
            return {"ok": True, "seconds": float(seconds), "path": path or "/tmp/test.wav"}

        def play_recording(self, path=None):
            return {"ok": True, "seconds": 1.25, "path": path or "/tmp/latest.wav"}

    svc._all_services = [_FakeAV()]
    r = client.post("/api/audio/playback", json={})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["seconds"] == pytest.approx(1.25)


def test_audio_mute_get_and_set(app_client):
    client, bus, svc = app_client

    class _FakeMusic:
        muted = False

        def set_muted(self, muted: bool):
            self.muted = bool(muted)

    svc._music_svc = _FakeMusic()

    r = client.get("/api/audio/mute")
    assert r.status_code == 200
    assert r.json()["muted"] is False

    r2 = client.put("/api/audio/mute", json={"muted": True})
    assert r2.status_code == 200
    assert r2.json()["muted"] is True
    assert svc._music_svc.muted is True


def test_audio_repeat_endpoint_calls_av_service(app_client):
    client, bus, svc = app_client

    class _FakeAV:
        name = "av"

        def repeat_last_spoken(self):
            return {"ok": True, "text": "hello again"}

    svc._all_services = [_FakeAV()]
    r = client.post("/api/audio/repeat")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["text"] == "hello again"


def test_put_privacy_settings_publishes_runtime_config(app_client):
    client, bus, svc = app_client
    events = []
    bus.subscribe("privacy.set_config", lambda t, p: events.append(p))

    r = client.put("/api/settings/privacy", json={
        "enabled": True,
        "threshold": 0.7,
        "cooldown_s": 5.0,
        "clear_frames": 4,
        "announce": False,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert events
    assert events[-1]["threshold"] == pytest.approx(0.7)
    assert events[-1]["cooldown_s"] == pytest.approx(5.0)
    assert events[-1]["clear_frames"] == 4
    assert events[-1]["announce"] is False


# ── Status API ────────────────────────────────────────────────────────────────

def test_status_returns_version(app_client):
    client, bus, svc = app_client
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    assert "last" in data


def test_status_includes_iot_snapshots(app_client):
    client, bus, svc = app_client

    class _FakeIoTRegistry:
        def get_all_snapshots(self):
            return {
                "radon": {
                    "available": True,
                    "device_id": "radon",
                    "device_name": "Radon Monitor",
                    "device_icon": "☢️",
                    "display": {"primary": {"value": "1.2", "unit": "pCi/L", "color": "#3fb950"}},
                    "history": [12.0, 14.0],
                }
            }

    svc._iot_registry = _FakeIoTRegistry()
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "iot" in data
    assert "radon" in data["iot"]
    assert data["iot"]["radon"]["device_name"] == "Radon Monitor"


# ── Dashboard HTML ────────────────────────────────────────────────────────────

def test_index_returns_html(app_client):
    client, _, _ = app_client
    r = client.get("/")
    assert r.status_code == 200
    assert "VERA" in r.text
    assert "<html" in r.text.lower()


# ── Servo settings API ────────────────────────────────────────────────────────

@pytest.fixture
def app_client_with_motion():
    bus = MessageBus()
    motion = MagicMock()
    motion.servo_enabled = True
    svc = WebService(bus=bus, port=18080, registry=_mock_registry(), motion_service=motion)
    app = svc._build_app()
    return TestClient(app), bus, svc, motion


def test_get_servo_enabled(app_client_with_motion):
    client, bus, svc, motion = app_client_with_motion
    r = client.get("/api/settings/servo")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_put_servo_disabled_publishes_to_bus(app_client_with_motion):
    client, bus, svc, motion = app_client_with_motion
    events = []
    bus.subscribe("motion.set_enabled", lambda t, p: events.append(p))
    r = client.put("/api/settings/servo", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert any(not e.get("enabled") for e in events)


def test_put_servo_enabled_publishes_to_bus(app_client_with_motion):
    client, bus, svc, motion = app_client_with_motion
    motion.servo_enabled = False
    events = []
    bus.subscribe("motion.set_enabled", lambda t, p: events.append(p))
    r = client.put("/api/settings/servo", json={"enabled": True})
    assert r.status_code == 200
    assert any(e.get("enabled") for e in events)


def test_get_servo_no_motion_svc_defaults_true():
    bus = MessageBus()
    svc = WebService(bus=bus, port=18080)
    app = svc._build_app()
    client = TestClient(app)
    r = client.get("/api/settings/servo")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_get_fan_control_points_from_thermal_runtime(tmp_path, monkeypatch):
    cfg = tmp_path / "thermal.yaml"
    cfg.write_text(
        """
thresholds:
  safe_max_c: 25.0
  warn_max_c: 47.0
  critical_c: 50.0
  fan_min_duty: 0.0
  fan_max_duty: 100.0
""".strip()
    )
    monkeypatch.setattr(web_service, "_THERMAL_CONFIG_PATH", cfg)
    monkeypatch.setattr(
        web_service,
        "_thermal_request",
        lambda req, timeout_ms=1500: {
            "ok": True,
            "control_points": [
                {"temp_c": 26.0, "duty": 10.0},
                {"temp_c": 49.0, "duty": 100.0},
            ],
        },
    )

    bus = MessageBus()
    svc = WebService(bus=bus, port=18080, registry=_mock_registry())
    client = TestClient(svc._build_app())

    r = client.get("/api/settings/fan/control-points")
    assert r.status_code == 200
    data = r.json()
    assert data["runtime_source"] == "thermal"
    assert data["control_points"][0]["temp_c"] == 26.0


def test_put_fan_control_points_persists_and_applies(tmp_path, monkeypatch):
    cfg = tmp_path / "thermal.yaml"
    cfg.write_text(
        """
thresholds:
  safe_max_c: 25.0
  warn_max_c: 47.0
  critical_c: 50.0
  fan_min_duty: 0.0
  fan_max_duty: 100.0
""".strip()
    )
    monkeypatch.setattr(web_service, "_THERMAL_CONFIG_PATH", cfg)

    calls = []

    def _fake_thermal_request(req, timeout_ms=1500):
        calls.append(req)
        return {"ok": True, "control_points": req.get("points", [])}

    monkeypatch.setattr(web_service, "_thermal_request", _fake_thermal_request)

    bus = MessageBus()
    svc = WebService(bus=bus, port=18080, registry=_mock_registry())
    client = TestClient(svc._build_app())

    payload = {
        "points": [
            {"temp_c": 24.0, "duty": 0.0},
            {"temp_c": 44.0, "duty": 70.0},
            {"temp_c": 50.0, "duty": 100.0},
        ]
    }
    r = client.put("/api/settings/fan/control-points", json=payload)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["runtime_applied"] is True
    assert calls and calls[0]["cmd"] == "fan_control_points.set"

    r2 = client.get("/api/settings/fan/control-points")
    assert r2.status_code == 200
    points = r2.json()["control_points"]
    assert points[0]["temp_c"] == 24.0
    assert points[-1]["duty"] == 100.0
