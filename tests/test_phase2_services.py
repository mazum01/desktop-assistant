"""Tests for vision/audio-capture/IPC services with mocked drivers."""

import json
import time
from unittest.mock import MagicMock

import numpy as np
import pytest

from src.core.bus import MessageBus
from src.services.audio_capture_service import AudioCaptureService
from src.services.vision_service import VisionService


# ── Vision ───────────────────────────────────────────────────────────────

class _FakeCamera:
    def __init__(self):
        self.hardware_ready = True
        self.started = False
        self.closed = False
        self._counter = 0
        self.stills = []
    def start(self):  self.started = True
    def close(self):  self.closed = True
    def capture_frame(self):
        self._counter += 1
        # Vary content so latest_frame() differs across ticks.
        return np.full((4, 4, 3), self._counter % 256, dtype=np.uint8)
    def capture_still(self, path):
        self.stills.append(path)


def test_vision_service_publishes_frame_metadata():
    bus = MessageBus()
    fake = _FakeCamera()
    svc = VisionService(bus=bus, camera=fake)
    svc.tick_seconds = 0.02

    metas = []
    bus.subscribe("vision.frame_ready", lambda t, p: metas.append(p))

    svc.start()
    time.sleep(0.1)
    svc.stop()

    assert fake.started and fake.closed
    assert metas, "expected at least one frame_ready event"
    assert metas[-1]["shape"] == (4, 4, 3)
    assert metas[-1]["index"] >= 1


def test_vision_service_latest_frame_accessor():
    bus = MessageBus()
    fake = _FakeCamera()
    svc = VisionService(bus=bus, camera=fake)
    svc.tick_seconds = 0.02
    svc.start()
    time.sleep(0.05)
    frame = svc.latest_frame()
    svc.stop()
    assert frame is not None
    assert frame.shape == (4, 4, 3)


def test_vision_service_capture_still_topic():
    bus = MessageBus()
    fake = _FakeCamera()
    svc = VisionService(bus=bus, camera=fake)
    svc.tick_seconds = 1.0  # avoid running ticks during this test
    svc.start()
    try:
        saved = []
        bus.subscribe("vision.still_saved", lambda t, p: saved.append(p))
        bus.publish("vision.capture_still", {"path": "/tmp/x.jpg"})
        assert fake.stills == ["/tmp/x.jpg"]
        assert saved and saved[0]["path"] == "/tmp/x.jpg"
    finally:
        svc.stop()


def test_vision_service_handles_capture_error():
    bus = MessageBus()
    fake = _FakeCamera()
    fake.capture_frame = MagicMock(side_effect=RuntimeError("boom"))
    svc = VisionService(bus=bus, camera=fake)
    svc.tick_seconds = 0.02

    errs = []
    bus.subscribe("vision.error", lambda t, p: errs.append(p))

    svc.start()
    time.sleep(0.05)
    svc.stop()

    assert errs and errs[0]["reason"] == "capture_failed"


# ── Audio capture ────────────────────────────────────────────────────────

class _FakeMic:
    def __init__(self, level=0.5):
        self.hardware_ready = True
        # Match real driver attribute name so the service can read sample_rate
        class _Cfg:
            sample_rate = 16000
        self._cfg = _Cfg()
        self._level = level
    def record(self, seconds):
        n = int(seconds * 16000)
        return np.full(n, self._level, dtype=np.float32)


def test_audio_capture_publishes_level_and_chunk():
    bus = MessageBus()
    mic = _FakeMic(level=0.5)
    svc = AudioCaptureService(bus=bus, mic=mic, chunk_seconds=0.02)

    levels, chunks = [], []
    bus.subscribe("audio.level", lambda t, p: levels.append(p))
    bus.subscribe("audio.chunk", lambda t, p: chunks.append(p))

    svc.start()
    time.sleep(0.1)
    svc.stop()

    assert levels and chunks
    # rms of constant 0.5 == 0.5 → -6 dBFS
    assert levels[-1]["dbfs"] == pytest.approx(-6.02, abs=0.1)
    assert chunks[-1]["rate"] == 16000
    assert chunks[-1]["samples"] > 0


def test_audio_capture_silence_is_floor_dbfs():
    bus = MessageBus()
    mic = _FakeMic(level=0.0)
    svc = AudioCaptureService(bus=bus, mic=mic, chunk_seconds=0.02)
    levels = []
    bus.subscribe("audio.level", lambda t, p: levels.append(p))
    svc.start()
    time.sleep(0.05)
    svc.stop()
    assert levels and levels[-1]["dbfs"] <= -100.0


def test_audio_capture_latest_chunk_accessor():
    bus = MessageBus()
    mic = _FakeMic(level=0.3)
    svc = AudioCaptureService(bus=bus, mic=mic, chunk_seconds=0.02)
    svc.start()
    time.sleep(0.05)
    chunk = svc.latest_chunk()
    svc.stop()
    assert chunk is not None
    assert chunk.dtype == np.float32


def test_audio_capture_handles_record_error():
    bus = MessageBus()
    mic = _FakeMic()
    mic.record = MagicMock(side_effect=RuntimeError("bus error"))
    svc = AudioCaptureService(bus=bus, mic=mic, chunk_seconds=0.02)
    errs = []
    bus.subscribe("audio.error", lambda t, p: errs.append(p))
    svc.start()
    time.sleep(0.05)
    svc.stop()
    assert errs and errs[0]["reason"] == "record_failed"


# ── IPC bridge ───────────────────────────────────────────────────────────

zmq = pytest.importorskip("zmq")
from src.services.ipc_bridge import IPCBridge


@pytest.fixture
def ipc_endpoints(tmp_path):
    pub = f"ipc://{tmp_path}/pub"
    rep = f"ipc://{tmp_path}/rep"
    return pub, rep


def test_ipc_bridge_starts_and_stops(ipc_endpoints):
    pub, rep = ipc_endpoints
    bus = MessageBus()
    svc = IPCBridge(bus=bus, pub_endpoint=pub, rep_endpoint=rep)
    svc.start()
    try:
        assert svc.enabled
    finally:
        svc.stop()


def test_ipc_bridge_forwards_bus_events_to_pub(ipc_endpoints):
    pub_ep, rep_ep = ipc_endpoints
    bus = MessageBus()
    svc = IPCBridge(bus=bus, pub_endpoint=pub_ep, rep_endpoint=rep_ep)
    svc.start()
    try:
        ctx = zmq.Context.instance()
        sub = ctx.socket(zmq.SUB)
        sub.connect(pub_ep)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        time.sleep(0.2)  # ZMQ slow-joiner

        bus.publish("test.topic", {"value": 42})

        sub.setsockopt(zmq.RCVTIMEO, 1000)
        topic, payload = sub.recv_multipart()
        sub.close(linger=0)

        assert topic == b"test.topic"
        assert json.loads(payload) == {"value": 42}
    finally:
        svc.stop()


def test_ipc_bridge_handles_publish_request(ipc_endpoints):
    pub_ep, rep_ep = ipc_endpoints
    bus = MessageBus()
    svc = IPCBridge(bus=bus, pub_endpoint=pub_ep, rep_endpoint=rep_ep)
    svc.start()
    try:
        received = []
        bus.subscribe("from_cli", lambda t, p: received.append(p))

        ctx = zmq.Context.instance()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 2000)
        req.connect(rep_ep)
        req.send_string(json.dumps({"cmd": "publish", "topic": "from_cli",
                                    "payload": {"hello": "world"}}))
        reply = json.loads(req.recv_string())
        req.close(linger=0)

        assert reply == {"ok": True}
        # tiny delay for sync delivery already done; received should be set.
        assert received and received[0] == {"hello": "world"}
    finally:
        svc.stop()


def test_ipc_bridge_ping_and_topics(ipc_endpoints):
    pub_ep, rep_ep = ipc_endpoints
    bus = MessageBus()
    bus.subscribe("foo", lambda t, p: None)
    svc = IPCBridge(bus=bus, pub_endpoint=pub_ep, rep_endpoint=rep_ep)
    svc.start()
    try:
        ctx = zmq.Context.instance()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 2000)
        req.connect(rep_ep)

        req.send_string(json.dumps({"cmd": "ping"}))
        assert json.loads(req.recv_string()) == {"ok": True, "pong": True}

        req.send_string(json.dumps({"cmd": "topics"}))
        reply = json.loads(req.recv_string())
        assert reply["ok"] is True
        assert "foo" in reply["topics"]

        req.close(linger=0)
    finally:
        svc.stop()


def test_ipc_bridge_handles_unknown_command(ipc_endpoints):
    pub_ep, rep_ep = ipc_endpoints
    bus = MessageBus()
    svc = IPCBridge(bus=bus, pub_endpoint=pub_ep, rep_endpoint=rep_ep)
    svc.start()
    try:
        ctx = zmq.Context.instance()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 2000)
        req.connect(rep_ep)
        req.send_string(json.dumps({"cmd": "nope"}))
        reply = json.loads(req.recv_string())
        req.close(linger=0)
        assert reply["ok"] is False
        assert "nope" in reply["error"]
    finally:
        svc.stop()


def test_ipc_bridge_status_reports_services_and_telemetry(ipc_endpoints):
    pub_ep, rep_ep = ipc_endpoints
    bus = MessageBus()
    svc = IPCBridge(bus=bus, pub_endpoint=pub_ep, rep_endpoint=rep_ep)
    svc.start()
    try:
        bus.publish("service.started", {"name": "thermal"})
        bus.publish("service.started", {"name": "motion"})
        bus.publish("thermal.temp", {"celsius": 38.5, "fahrenheit": 101.3, "ok": True})
        bus.publish("motion.position", {"angle": 75.0})

        ctx = zmq.Context.instance()
        req = ctx.socket(zmq.REQ)
        req.setsockopt(zmq.RCVTIMEO, 2000)
        req.connect(rep_ep)
        req.send_string(json.dumps({"cmd": "status"}))
        reply = json.loads(req.recv_string())
        req.close(linger=0)

        assert reply["ok"] is True
        s = reply["status"]
        assert "version" in s
        assert s["uptime_s"] >= 0.0
        assert s["services"]["thermal"]["running"] is True
        assert s["services"]["motion"]["running"] is True
        assert s["last"]["thermal.temp"]["celsius"] == 38.5
        assert s["last"]["motion.position"]["angle"] == 75.0
        assert s["endpoints"]["pub"] == pub_ep
    finally:
        svc.stop()
