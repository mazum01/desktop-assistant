"""Integration tests for Phase 3 of the process-isolation proposal: the
`web` process (`WebService`, the FastAPI dashboard/API) running as a real,
independent `ProcessNode`.

Unlike Phase 1 (`media`)/Phase 2b (`integrations`), the direction of the
proxy relationship is reversed here: `web_main.py` builds proxies
(`src/core/web_client.py`) pointed *at* a fake "core" node that registers
RPC handlers the same way `core_main.py` does, and `WebService` itself is
what moved out. These tests prove:
  1. The RPC round trip works end-to-end over real ZeroMQ for a
     representative sample of the 10 proxies (room, motion, vision jpeg/
     snapshot, camera2 `is_configured()` caching, depth flags) — not every
     single one, since the underlying `IPCClient.call()`/`register_rpc()`
     mechanism itself is already covered generically by
     `test_process_node.py`.
  2. Bus events flow in both directions across the process boundary:
     `WebService` publishing on its own bus (e.g. `motion.pan_to`) reaches
     "core"; and a core-originated event (e.g. `face.greeted`) reaches the
     web process's bus, mirroring the 4-way upstream subscription
     `web_main.py` sets up in production (core, thermal, media,
     integrations).
  3. `Camera2ServiceProxy.is_configured()` caches its answer after the
     first successful round trip instead of re-hitting the wire on every
     call, as documented in `web_client.py`.

See docs/architecture/PROCESS_ISOLATION_PROPOSAL.md §6 (Phase 3).
"""

import base64
import time
import uuid

import pytest

pytest.importorskip("zmq")

from src.assistant.web_main import build_node
from src.core.ipc_client import IPCClient
from src.core.process_node import ProcessNode
from src.core.web_client import (
    Camera2ServiceProxy,
    DepthServicesProxy,
    MotionServiceProxy,
    RoomServiceProxy,
    VisionServiceProxy,
)


def _unique_endpoints(prefix: str):
    tag = uuid.uuid4().hex[:8]
    return (
        f"ipc:///tmp/test-{prefix}-{tag}.pub",
        f"ipc:///tmp/test-{prefix}-{tag}.rep",
    )


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


class _FakeCoreServices:
    """Stands in for the handful of real service objects `core_main.py`
    registers RPC handlers against — just enough state to prove the wire
    format/round trip, not real camera/motion hardware."""

    def __init__(self):
        self.room_status = {"name": "office", "anthropic_enabled": True}
        self.servo_enabled = True
        self.soft_min_deg = 135.0
        self.soft_max_deg = 215.0
        self.jpeg_calls = 0
        self.camera2_status_calls = 0
        self.camera2_configured = True
        self.dense_enabled = True
        self.mono_enabled = False


@pytest.fixture
def web_and_core(tmp_path, monkeypatch):
    """A real `web` ProcessNode (built via `web_main.build_node()`, web
    dashboard itself disabled so no uvicorn server binds a port in tests)
    plus a fake "core"/"thermal"/"media"/"integrations" node it
    upstream-subscribes to, and a fake "core" REP endpoint registering the
    same RPC command names `core_main.py` does."""
    core_pub, core_rep = _unique_endpoints("core")
    thermal_pub, _thermal_rep = _unique_endpoints("thermal")
    media_pub, media_rep = _unique_endpoints("media")
    integ_pub, integ_rep = _unique_endpoints("integrations")
    web_pub, web_rep = _unique_endpoints("web")

    core = ProcessNode(
        name="core", pub_endpoint=core_pub, rep_endpoint=core_rep,
        upstream_endpoints=[web_pub],  # mirrors core_main.py's _WEB_PUB addition
    )
    thermal = ProcessNode(name="thermal", pub_endpoint=thermal_pub, rep_endpoint=_thermal_rep)
    media = ProcessNode(name="media", pub_endpoint=media_pub, rep_endpoint=media_rep)
    integrations = ProcessNode(name="integrations", pub_endpoint=integ_pub, rep_endpoint=integ_rep)

    fakes = _FakeCoreServices()

    def _rpc_room_get_status(_msg):
        return {"ok": True, "status": fakes.room_status}

    def _rpc_motion_get_status(_msg):
        return {
            "ok": True,
            "status": {
                "servo_enabled": fakes.servo_enabled,
                "soft_min_deg": fakes.soft_min_deg,
                "soft_max_deg": fakes.soft_max_deg,
            },
        }

    def _rpc_vision_latest_jpeg(_msg):
        fakes.jpeg_calls += 1
        return {"ok": True, "jpeg_b64": base64.b64encode(b"\xff\xd8fake-jpeg-bytes").decode("ascii")}

    def _rpc_camera2_get_status(_msg):
        fakes.camera2_status_calls += 1
        if not fakes.camera2_configured:
            return {"ok": True, "configured": False, "status": {}}
        return {
            "ok": True,
            "configured": True,
            "status": {"rotation_deg": 0, "resolution": [1280, 720], "stream_resolution": [640, 360]},
        }

    def _rpc_depth_get_enabled_flags(_msg):
        return {"ok": True, "flags": {"dense_enabled": fakes.dense_enabled, "mono_enabled": fakes.mono_enabled}}

    core.ipc.register_rpc("room.get_status", _rpc_room_get_status)
    core.ipc.register_rpc("motion.get_status", _rpc_motion_get_status)
    core.ipc.register_rpc("vision.latest_jpeg", _rpc_vision_latest_jpeg)
    core.ipc.register_rpc("camera2.get_status", _rpc_camera2_get_status)
    core.ipc.register_rpc("depth.get_enabled_flags", _rpc_depth_get_enabled_flags)

    node = build_node(
        cfg={"web_dashboard": {"enabled": False}},  # no uvicorn server needed for these tests
        pub_endpoint=web_pub,
        rep_endpoint=web_rep,
        upstream_endpoints=[core_pub, thermal_pub, media_pub, integ_pub],
        core_rep=core_rep,
        media_rep=media_rep,
        integrations_rep=integ_rep,
    )

    for n in (core, thermal, media, integrations):
        for svc in n.services:
            svc.start()
    for svc in node.services:
        svc.start()

    time.sleep(0.3)

    try:
        yield core, node, core_rep, fakes
    finally:
        for svc in reversed(node.services):
            svc.stop()
        for n in (integrations, media, thermal, core):
            for svc in reversed(n.services):
                svc.stop()


def test_room_status_rpc_round_trips_through_proxy(web_and_core):
    _core, _node, core_rep, _fakes = web_and_core
    client = IPCClient(core_rep, timeout_ms=2000)
    proxy = RoomServiceProxy(client)

    status = proxy.get_status()
    assert status == {"name": "office", "anthropic_enabled": True}


def test_motion_status_rpc_round_trips_through_proxy(web_and_core):
    _core, _node, core_rep, _fakes = web_and_core
    client = IPCClient(core_rep, timeout_ms=2000)
    proxy = MotionServiceProxy(client)

    status = proxy.get_status()
    assert status["servo_enabled"] is True
    assert status["soft_min_deg"] == 135.0
    assert status["soft_max_deg"] == 215.0


def test_vision_latest_jpeg_rpc_round_trips_and_decodes_base64(web_and_core):
    _core, _node, core_rep, fakes = web_and_core
    client = IPCClient(core_rep, timeout_ms=2000)
    proxy = VisionServiceProxy(client)

    jpeg = proxy.latest_jpeg()
    assert jpeg == b"\xff\xd8fake-jpeg-bytes"
    assert fakes.jpeg_calls == 1


def test_depth_get_enabled_flags_rpc_round_trips(web_and_core):
    _core, _node, core_rep, _fakes = web_and_core
    client = IPCClient(core_rep, timeout_ms=2000)
    proxy = DepthServicesProxy(client)

    flags = proxy.get_enabled_flags()
    assert flags == {"dense_enabled": True, "mono_enabled": False}


def test_camera2_is_configured_caches_after_first_success(web_and_core):
    """`Camera2ServiceProxy.is_configured()` should hit the wire exactly
    once and cache the answer, since cam2's presence never changes at
    runtime — avoiding an RPC round trip on every truthiness check
    `web_service.py` makes (startup subscribe gate, GET routes, etc.)."""
    _core, _node, core_rep, fakes = web_and_core
    client = IPCClient(core_rep, timeout_ms=2000)
    proxy = Camera2ServiceProxy(client)

    assert proxy.is_configured() is True
    assert proxy.is_configured() is True
    assert proxy.is_configured() is True
    assert fakes.camera2_status_calls == 1


def test_camera2_is_configured_does_not_cache_transport_failure():
    """If core is unreachable, `is_configured()` must return False without
    caching that failure, so a later successful call (once core comes back
    up) is retried instead of permanently reporting cam2 as absent."""
    client = IPCClient("ipc:///tmp/test-nobody-home.rep", timeout_ms=200)
    proxy = Camera2ServiceProxy(client)

    assert proxy.is_configured() is False
    assert proxy.is_configured() is False  # still retries, not cached as False


def test_motion_pan_published_by_web_reaches_core(web_and_core):
    """Proves the reverse direction: an action `WebService`'s `/api/pan`
    route publishes on its own bus (`motion.pan_to`) is forwarded upstream
    to "core", where `MotionService` (which stays in core) would normally
    pick it up via its existing `bus.subscribe(...)` handler."""
    core, node, _core_rep, _fakes = web_and_core

    received = []
    core.bus.subscribe("motion.pan_to", lambda _t, payload: received.append(payload))

    node.bus.publish("motion.pan_to", {"angle": 90, "override_quiet": True})

    assert _wait_until(lambda: len(received) == 1)
    assert received[0]["angle"] == 90


def test_core_originated_event_reaches_web_process(web_and_core):
    """face.greeted is published on core's local bus (FaceService, which
    stays in core); proves it's forwarded to the web process, where
    WebService's status snapshot / SSE stream would normally pick it up."""
    core, node, _core_rep, _fakes = web_and_core

    received = []
    node.bus.subscribe("face.greeted", lambda _t, payload: received.append(payload))

    core.bus.publish("face.greeted", {"face_id": "abc", "name": "Ada", "text": "Hi Ada!"})

    assert _wait_until(lambda: len(received) == 1)
    assert received[0]["name"] == "Ada"


def test_proxy_falls_back_gracefully_when_core_process_unreachable():
    """If core isn't running (e.g. mid-restart), read-only proxy methods
    should degrade to safe defaults instead of raising — matching the old
    "if not self._room_svc: <default>" behavior WebService relied on when
    it shared core's process directly."""
    client = IPCClient("ipc:///tmp/test-nobody-home.rep", timeout_ms=200)

    assert RoomServiceProxy(client).get_status() == {}
    assert MotionServiceProxy(client).get_status() == {}
    assert VisionServiceProxy(client).latest_jpeg() is None
    assert DepthServicesProxy(client).get_enabled_flags() == {
        "dense_enabled": False, "mono_enabled": False,
    }
