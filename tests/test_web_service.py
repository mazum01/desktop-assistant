"""Tests for WebService REST endpoints using FastAPI TestClient."""
import json
import pathlib
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


def test_put_custom_eq_persists_band_curve_and_get_returns_it(app_client, tmp_path, monkeypatch):
    client, bus, svc = app_client
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    bands = [
        {"hz": 80, "gain_db": 4.0, "q": 1.0},
        {"hz": 1000, "gain_db": -2.0, "q": 1.2},
    ]

    r = client.put("/api/music/eq/custom", json={"bands": bands})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    state_file = tmp_path / ".config" / "desktop-assistant" / "custom_eq.json"
    assert state_file.exists()
    assert json.loads(state_file.read_text()) == bands

    r2 = client.get("/api/music/eq/custom")
    assert r2.status_code == 200
    assert r2.json()["bands"] == bands


def test_start_seeds_service_states_from_remote_node_status(monkeypatch):
    class _FakeClient:
        def __init__(self, reply):
            self.reply = reply
            self.calls = []

        def call(self, request, timeout_ms=None):
            self.calls.append((request, timeout_ms))
            return self.reply

    bus = MessageBus()
    core_client = _FakeClient({
        "ok": True,
        "status": {
            "services": {
                "vision": {"running": True, "ts": time.time()},
                "tracking": {"running": True, "error": True, "ts": time.time()},
                "room": {"running": False, "ts": time.time()},
            }
        },
    })
    media_client = _FakeClient({
        "ok": True,
        "status": {
            "services": {
                "music": {"running": True, "ts": time.time()},
            }
        },
    })

    monkeypatch.setattr(WebService, "_run_server", lambda self: None)

    svc = WebService(
        bus=bus,
        port=18080,
        registry=_mock_registry(),
        status_clients={"core": core_client, "media": media_client},
    )
    svc.start()
    try:
        assert svc._service_states["vision"] == "running"
        assert svc._service_states["tracking"] == "error"
        assert svc._service_states["room"] == "stopped"
        assert svc._service_states["music"] == "running"
        assert core_client.calls == [({"cmd": "status"}, None)]
        assert media_client.calls == [({"cmd": "status"}, None)]
    finally:
        svc.stop()


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




def test_audio_voice_gain_get_and_set_persists_and_applies_runtime(app_client, tmp_path, monkeypatch):
    client, bus, svc = app_client

    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text("tts:\n  output_gain: 1.0\n")
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)

    class _FakeAV:
        name = "av"

        def __init__(self):
            self.gain = 1.0

        def get_voice_output_gain(self):
            return self.gain

        def set_voice_output_gain(self, gain: float):
            self.gain = float(gain)
            return self.gain

    fake_av = _FakeAV()
    svc._all_services = [fake_av]

    r = client.get("/api/audio/voice-gain")
    assert r.status_code == 200
    assert r.json()["level"] == 100

    r2 = client.put("/api/audio/voice-gain", json={"level": 150})
    assert r2.status_code == 200
    assert r2.json()["level"] == 150
    assert fake_av.gain == pytest.approx(1.5)

    txt = cfg_path.read_text()
    assert "output_gain: 1.5" in txt


def test_audio_spectrum_test_endpoint_calls_av_service_with_latest_spectrum(app_client):
    client, bus, svc = app_client

    class _FakeAV:
        name = "av"

        def __init__(self):
            self.calls = []

        def play_spectrum_test(self, bins=48, sample_rate=16000, max_hz=8000.0):
            self.calls.append(
                {"bins": bins, "sample_rate": sample_rate, "max_hz": max_hz}
            )
            return {"ok": True, "bins": bins, "sample_rate": sample_rate, "max_hz": max_hz}

    fake_av = _FakeAV()
    svc._all_services = [fake_av]
    bus.publish("audio.spectrum", {"bins": [0.0] * 24, "sample_rate": 16000, "max_hz": 8000.0})

    r = client.post("/api/audio/spectrum-test")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["bins"] == 24
    assert fake_av.calls[-1]["sample_rate"] == 16000
    assert fake_av.calls[-1]["max_hz"] == pytest.approx(8000.0)


def test_audio_spectrum_test_requires_respeaker_processing_disabled(app_client, tmp_path, monkeypatch):
    client, _bus, svc = app_client

    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text(
        "audio:\n"
        "  backend: respeaker_flex\n"
        "  default: {}\n"
        "  respeaker_flex:\n"
        "    input_device_name: ReSpeaker\n"
        "    input_sample_rate: 16000\n"
        "    input_raw_channels: 6\n"
        "    input_processing_enabled: true\n"
        "    input_processed_channel: 0\n"
        "    input_raw_mic_channel: 1\n"
        "    output_alsa_device: pulse\n"
        "    output_sample_rate: 44100\n"
        "    loudness_boost: 2.5\n"
        "    eq_preset: flat\n"
        "    led_enabled: false\n"
    )
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)

    class _FakeAV:
        name = "av"

        def __init__(self):
            self.called = False

        def play_spectrum_test(self, bins=48, sample_rate=16000, max_hz=8000.0):
            self.called = True
            return {"ok": True, "bins": bins, "sample_rate": sample_rate, "max_hz": max_hz}

    fake_av = _FakeAV()
    svc._all_services = [fake_av]

    r = client.post("/api/audio/spectrum-test")
    assert r.status_code == 409
    assert "mic isolation is enabled" in r.json()["detail"]
    assert fake_av.called is False


def test_audio_settings_respeaker_processing_toggle_persists(app_client, tmp_path, monkeypatch):
    client, _bus, _svc = app_client
    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text(
        "audio:\n"
        "  backend: respeaker_flex\n"
        "  default: {}\n"
        "  respeaker_flex:\n"
        "    input_device_name: ReSpeaker\n"
        "    input_sample_rate: 16000\n"
        "    input_raw_channels: 6\n"
        "    input_processing_enabled: true\n"
        "    input_processed_channel: 0\n"
        "    input_raw_mic_channel: 1\n"
        "    output_alsa_device: pulse\n"
        "    output_sample_rate: 16000\n"
        "    loudness_boost: 2.5\n"
        "    eq_preset: flat\n"
        "    led_enabled: false\n"
    )
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)

    r = client.put(
        "/api/settings/audio",
        json={
            "backend": "respeaker_flex",
            "respeaker_flex": {
                "input_device_name": "ReSpeaker",
                "input_sample_rate": 16000,
                "input_raw_channels": 6,
                "input_processing_enabled": False,
                "input_processed_channel": 0,
                "input_raw_mic_channel": 2,
                "output_alsa_device": "pulse",
                "output_sample_rate": 16000,
                "loudness_boost": 2.5,
                "eq_preset": "flat",
                "led_enabled": False,
            },
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["respeaker_flex"]["input_processing_enabled"] is False
    assert data["respeaker_flex"]["input_raw_mic_channel"] == 2

    text = cfg_path.read_text()
    assert "input_processing_enabled: false" in text
    assert "input_raw_mic_channel: 2" in text


def test_voice_settings_get_and_put_persist_and_publish(app_client, tmp_path, monkeypatch):
    client, bus, _svc = app_client
    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text(
        "voice_commands:\n"
        "  enabled: false\n"
        "  stt_backend: shell\n"
        "  stt_command: \"\"\n"
        "  stt_language: en\n"
        "  stt_timeout_s: 20.0\n"
    )
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)
    events = []
    bus.subscribe("voice.set_config", lambda _t, p: events.append(p))

    r = client.get("/api/settings/voice")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    assert "faster_whisper" in r.json()["available_stt_backends"]
    assert "shell" in r.json()["available_stt_backends"]

    r2 = client.put(
        "/api/settings/voice",
        json={
            "enabled": True,
            "stt_backend": "shell",
            "stt_language": "en",
            "stt_command": "echo hello",
            "stt_timeout_s": 12.5,
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["enabled"] is True
    assert body["stt_command"] == "echo hello"
    assert body["runtime_applied"] is True
    assert events
    assert events[-1]["enabled"] is True

    text = cfg_path.read_text()
    assert "enabled: true" in text
    assert "stt_command: echo hello" in text


def test_voice_settings_reject_invalid_backend(app_client):
    client, _bus, _svc = app_client
    r = client.put("/api/settings/voice", json={"stt_backend": "bad_backend"})
    assert r.status_code == 422


class _FakeXvfController:
    def __init__(self):
        self.saved = False
        self.tunables = {
            "PP_AGCONOFF": [True],
            "AUDIO_MGR_MIC_GAIN": [1.5],
        }
        self.writes = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def write(self, command, values):
        self.writes.append((command, values))
        self.tunables[command] = values

    def save_configuration(self):
        self.saved = True

    def snapshot(self):
        return {
            "connected": True,
            "usb": {"vendor_id": "0x2886", "product_id": "0x0018", "bus": 1, "address": 2},
            "readonly": [
                {"command": "VERSION", "label": "Firmware Version", "values": ["1.0.0"]},
            ],
            "tunables": [
                {
                    "command": "PP_AGCONOFF",
                    "label": "AGC Enabled",
                    "dtype": "bool",
                    "values": self.tunables["PP_AGCONOFF"],
                    "min": None,
                    "max": None,
                },
                {
                    "command": "AUDIO_MGR_MIC_GAIN",
                    "label": "Mic Gain",
                    "dtype": "float",
                    "values": self.tunables["AUDIO_MGR_MIC_GAIN"],
                    "min": 0.0,
                    "max": 10.0,
                },
            ],
        }


def test_xvf_get_returns_unavailable_when_controller_missing(app_client, monkeypatch):
    client, _bus, _svc = app_client
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: None)

    r = client.get("/api/audio/xvf")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["readonly"] == []
    assert body["tunables"] == []


class _PermissionDeniedXvfController:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def snapshot(self):
        raise PermissionError("permission denied opening USB device")

    def save_configuration(self):
        raise PermissionError("permission denied opening USB device")

    def write(self, command, values):
        raise PermissionError("permission denied opening USB device")


def test_xvf_get_returns_unavailable_when_permission_denied(app_client, monkeypatch):
    client, _bus, _svc = app_client
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: _PermissionDeniedXvfController())

    r = client.get("/api/audio/xvf")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["connected"] is False
    assert body["error"] == "permission_denied"


def test_xvf_put_applies_writes_and_can_save(app_client, monkeypatch):
    client, _bus, _svc = app_client
    fake = _FakeXvfController()
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: fake)

    r = client.put(
        "/api/audio/xvf",
        json={
            "writes": [
                {"command": "PP_AGCONOFF", "values": [False]},
                {"command": "AUDIO_MGR_MIC_GAIN", "values": [2.25]},
            ],
            "save": True,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["saved"] is True
    assert fake.saved is True
    assert ("PP_AGCONOFF", [False]) in fake.writes
    assert ("AUDIO_MGR_MIC_GAIN", [2.25]) in fake.writes
    tunables = {item["command"]: item["values"] for item in body["tunables"]}
    assert tunables["PP_AGCONOFF"] == [False]
    assert tunables["AUDIO_MGR_MIC_GAIN"] == [2.25]


def test_xvf_put_returns_503_when_permission_denied(app_client, monkeypatch):
    client, _bus, _svc = app_client
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: _PermissionDeniedXvfController())

    r = client.put(
        "/api/audio/xvf",
        json={"writes": [{"command": "PP_AGCONOFF", "values": [False]}], "save": False},
    )
    assert r.status_code == 503
    assert "access denied" in r.json()["detail"].lower()


def test_xvf_save_endpoint_persists_current_configuration(app_client, monkeypatch):
    client, _bus, _svc = app_client
    fake = _FakeXvfController()
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: fake)

    r = client.post("/api/audio/xvf/save")
    assert r.status_code == 200
    assert r.json()["saved"] is True
    assert fake.saved is True


def test_xvf_save_returns_503_when_permission_denied(app_client, monkeypatch):
    client, _bus, _svc = app_client
    monkeypatch.setattr(web_service, "_create_xvf_controller", lambda: _PermissionDeniedXvfController())

    r = client.post("/api/audio/xvf/save")
    assert r.status_code == 503
    assert "access denied" in r.json()["detail"].lower()


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
        "rate_hz": 1.0,
        "idle_rate_hz": 0.25,
        "threshold": 0.7,
        "cooldown_s": 5.0,
        "clear_frames": 4,
        "require_person": True,
        "person_hold_s": 8.0,
        "announce": False,
    })
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert events
    assert events[-1]["threshold"] == pytest.approx(0.7)
    assert events[-1]["rate_hz"] == pytest.approx(1.0)
    assert events[-1]["idle_rate_hz"] == pytest.approx(0.25)
    assert events[-1]["cooldown_s"] == pytest.approx(5.0)
    assert events[-1]["clear_frames"] == 4
    assert events[-1]["require_person"] is True
    assert events[-1]["person_hold_s"] == pytest.approx(8.0)
    assert events[-1]["announce"] is False


def test_podcast_list_and_status_endpoints(app_client):
    client, bus, svc = app_client

    class _FakePodcast:
        subscriptions = [{"id": "p1", "title": "Daily", "author": "News"}]

        def status(self):
            return {"ok": True, "state": "stopped", "subscriptions": 1, "player": None}

    svc._podcast_svc = _FakePodcast()

    r = client.get("/api/podcasts")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["subscriptions"][0]["id"] == "p1"

    r2 = client.get("/api/podcasts/status")
    assert r2.status_code == 200
    assert r2.json()["state"] == "stopped"


def test_podcast_play_endpoint_calls_service(app_client):
    client, bus, svc = app_client
    called = {}

    class _FakePodcast:
        def play(self, podcast_id, episode_index):
            called["podcast_id"] = podcast_id
            called["episode_index"] = episode_index
            return {"ok": True, "state": "playing", "podcast_title": "Daily", "episode_title": "Ep1"}

    svc._podcast_svc = _FakePodcast()
    r = client.post("/api/podcasts/play", json={"podcast_id": "p1", "episode_index": 2})
    assert r.status_code == 200
    assert called["podcast_id"] == "p1"
    assert called["episode_index"] == 2
    assert r.json()["state"] == "playing"


def test_podcast_seek_and_skip_endpoints_call_service(app_client):
    client, bus, svc = app_client
    called = {}

    class _FakePodcast:
        def seek(self, position_sec):
            called["seek"] = float(position_sec)
            return {"ok": True, "state": "playing", "position_sec": float(position_sec)}

        def skip(self, delta_sec):
            called["skip"] = float(delta_sec)
            return {"ok": True, "state": "playing", "position_sec": 42.0}

    svc._podcast_svc = _FakePodcast()

    r = client.post("/api/podcasts/seek", json={"position_sec": 123.5})
    assert r.status_code == 200
    assert called["seek"] == pytest.approx(123.5)
    assert r.json()["position_sec"] == pytest.approx(123.5)

    r2 = client.post("/api/podcasts/skip", json={"delta_sec": -15})
    assert r2.status_code == 200
    assert called["skip"] == pytest.approx(-15.0)


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


def test_iot_action_route_dispatches_to_device(app_client):
    client, bus, svc = app_client

    class _FakeIoTRegistryProxy:
        def execute_action(self, device_id, action, params=None):
            assert device_id == "nest_thermostat"
            assert action == "auth"
            return {
                "ok": True,
                "payload": {
                    "ok": True,
                    "message": "https://example.invalid/auth",
                    "auth_url": "https://example.invalid/auth",
                },
            }

    svc._iot_registry = _FakeIoTRegistryProxy()
    r = client.post("/api/iot/nest_thermostat/action", json={"action": "auth", "params": {}})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["auth_url"] == "https://example.invalid/auth"


def test_iot_config_route_merges_and_restarts_device(app_client, monkeypatch):
    client, bus, svc = app_client
    calls = {"stop": 0, "start": 0, "saved": 0}

    class _FakeIoTRegistryProxy:
        def __init__(self):
            self._cfg = {
                "project_id": "proj",
                "client_id": "cid",
                "client_secret": "secret",
            }

        def update_config(self, device_id, config_patch):
            assert device_id == "nest_thermostat"
            calls["stop"] += 1
            self._cfg.update(config_patch)
            calls["start"] += 1
            calls["saved"] += 1
            return {"ok": True, "device_id": device_id, "config": dict(self._cfg)}

    svc._iot_registry = _FakeIoTRegistryProxy()
    r = client.put("/api/iot/nest_thermostat", json={"config": {"refresh_token": "newtoken"}})
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["config"]["project_id"] == "proj"
    assert data["config"]["refresh_token"] == "newtoken"
    assert calls["stop"] == 1
    assert calls["start"] == 1
    assert calls["saved"] == 1


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
    motion.get_status.return_value = {"servo_enabled": True, "soft_min_deg": 135.0, "soft_max_deg": 215.0}
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
    motion.get_status.return_value = {"servo_enabled": False, "soft_min_deg": 135.0, "soft_max_deg": 215.0}
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


# ── Camera 2 frame subscription boot-race regression ──────────────────────────

def test_frame2_subscription_survives_camera2_configured_check_failing_at_boot():
    """Regression test for a real bug: at boot, this web process can start
    (and reach WebService.start()) before core's raw_camera2 service has
    registered its camera2.get_status RPC handler. The vision.frame2_ready
    bus subscription used to be gated behind `if self._camera2_configured():`
    — a one-shot check performed only once during start() — so a transient
    RPC failure at that exact moment permanently skipped the subscription
    for the process's whole lifetime, even though cam2 came up fine moments
    later (confirmed live: /api/snapshot2 and rotation endpoints worked,
    because they re-check on every request; only the /stream2 MJPEG feed
    was silently broken, stuck at 0 fps).

    Simulates that race with a camera2 proxy whose is_configured() fails for
    the first few calls (mirrors real RPC timeouts before core's raw_camera2
    has started) and succeeds afterwards. Asserts on the true observable
    symptom — whether a frame published once camera2 is up actually reaches
    `_latest_frame2` — rather than exact call counts, so it fails under the
    old gated-subscription code (subscription was already skipped in
    start(), so no frame ever arrives) and passes under the fix
    (subscription is unconditional, so the frame flows once camera2 answers).
    """
    bus = MessageBus()
    camera2 = MagicMock()
    failures_before_ready = 3
    calls = {"n": 0}

    def _is_configured():
        calls["n"] += 1
        return calls["n"] > failures_before_ready

    camera2.is_configured.side_effect = _is_configured
    camera2.latest_jpeg.return_value = b"\xff\xd8fake-jpeg"

    svc = WebService(bus=bus, port=18099, registry=_mock_registry(), camera2_service=camera2)
    try:
        svc.start()
        # Wait for the server thread's event loop (and _frame2_event) to exist.
        deadline = time.time() + 3.0
        while svc._loop is None and time.time() < deadline:
            time.sleep(0.02)
        assert svc._loop is not None, "server thread never initialized its event loop"

        # Exhaust the "still not ready" window — represents any polling
        # (old start()'s gating check, other routes, etc.) that might have
        # observed is_configured() == False before core's raw_camera2 truly
        # came up.
        for _ in range(failures_before_ready):
            svc._camera2_configured()

        bus.publish("vision.frame2_ready", {"index": 1, "ts": time.time()})
        deadline = time.time() + 2.0
        while svc._latest_frame2 is None and time.time() < deadline:
            time.sleep(0.02)

        assert svc._latest_frame2 == b"\xff\xd8fake-jpeg"
        assert svc._cam2_frame_count == 1
    finally:
        svc.stop()


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


# ── Anthropic API settings ────────────────────────────────────────────────────


def test_get_anthropic_settings_defaults_true_with_no_services(app_client):
    client, bus, svc = app_client
    r = client.get("/api/settings/anthropic")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_get_anthropic_settings_reflects_room_service_state(app_client):
    client, bus, svc = app_client

    class _FakeRoom:
        name = "room"

        def get_status(self):
            return {"anthropic_enabled": False}

    svc._room_svc = _FakeRoom()
    r = client.get("/api/settings/anthropic")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_get_anthropic_settings_falls_back_to_face_service(app_client):
    client, bus, svc = app_client

    class _FakeFace:
        name = "face"

        def get_anthropic_enabled(self, default: bool = True):
            return False

    svc._room_svc = None
    svc._face_svc = _FakeFace()
    r = client.get("/api/settings/anthropic")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_put_anthropic_settings_publishes_bus_event(app_client, tmp_path, monkeypatch):
    client, bus, svc = app_client
    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text("audio:\n  backend: respeaker_flex\n")
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)

    events = []
    bus.subscribe("anthropic.set_enabled", lambda t, p: events.append(p))

    r = client.put("/api/settings/anthropic", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert events
    assert events[-1]["enabled"] is False


def test_put_anthropic_settings_persists_to_yaml(app_client, tmp_path, monkeypatch):
    client, bus, svc = app_client

    cfg_path = tmp_path / "assistant.yaml"
    cfg_path.write_text("audio:\n  backend: respeaker_flex\n")
    monkeypatch.setattr(web_service, "_ASSISTANT_CONFIG_PATH", cfg_path)

    r = client.put("/api/settings/anthropic", json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    txt = cfg_path.read_text()
    assert "anthropic_api" in txt
    assert "enabled: false" in txt


def test_put_anthropic_settings_missing_enabled_returns_error(app_client):
    client, bus, svc = app_client
    r = client.put("/api/settings/anthropic", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False
