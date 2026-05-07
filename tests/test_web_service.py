"""Tests for WebService REST endpoints using FastAPI TestClient."""
import time
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from src.services.web_service import WebService
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


# ── Status API ────────────────────────────────────────────────────────────────

def test_status_returns_version(app_client):
    client, bus, svc = app_client
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    assert "last" in data


# ── Dashboard HTML ────────────────────────────────────────────────────────────

def test_index_returns_html(app_client):
    client, _, _ = app_client
    r = client.get("/")
    assert r.status_code == 200
    assert "Desktop Assistant" in r.text
    assert "<html" in r.text.lower()
