"""Tests for WebService REST endpoints using FastAPI TestClient."""
import time
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

from src.services.web_service import WebService
from src.core.bus import MessageBus


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _mock_registry():
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
    return r


@pytest.fixture
def app_client():
    bus = MessageBus()
    svc = WebService(bus=bus, port=18080, registry=_mock_registry())
    # Build the app directly (without running uvicorn)
    app = svc._build_app()
    return TestClient(app), bus, svc


# ── Faces API ────────────────────────────────────────────────────────────────

def test_get_faces_returns_list(app_client):
    client, bus, svc = app_client
    r = client.get("/api/faces")
    assert r.status_code == 200
    data = r.json()
    assert "faces" in data
    assert len(data["faces"]) == 1
    assert data["faces"][0]["name"] == "Alice"


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
