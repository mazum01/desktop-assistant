"""Tests for ProcessNode / IPCClient — the reusable cross-process bus
scaffolding proposed in docs/architecture/PROCESS_ISOLATION_PROPOSAL.md.

These tests bind two independent ProcessNode instances to distinct ZeroMQ
`ipc://` socket paths (the same transport real separate OS processes use)
and prove that:
  1. events published on node B (with node A configured as its upstream)
     are visible on node A's own bus — the exact mechanism that lets the
     `core` process see `thermal.*` telemetry today;
  2. RPC calls placed via IPCClient reach a handler registered on the
     target node and return its reply.

Skipped automatically if `pyzmq` isn't installed (mirrors IPCBridge's own
soft-dependency behavior).
"""

import time
import uuid

import pytest

pytest.importorskip("zmq")

from src.core.ipc_client import IPCClient
from src.core.process_node import ProcessNode


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


@pytest.fixture
def two_nodes():
    """Node 'downstream' has node 'upstream' as its... upstream.

    i.e. events published on `upstream` are forwarded into `downstream`'s
    bus — mirroring how core subscribes to thermal's PUB socket.
    """
    up_pub, up_rep = _unique_endpoints("upstream")
    down_pub, down_rep = _unique_endpoints("downstream")

    upstream = ProcessNode(name="upstream", pub_endpoint=up_pub, rep_endpoint=up_rep)
    downstream = ProcessNode(
        name="downstream",
        pub_endpoint=down_pub,
        rep_endpoint=down_rep,
        upstream_endpoints=[up_pub],
    )

    for svc in upstream.services:
        svc.start()
    for svc in downstream.services:
        svc.start()

    # Give the SUB socket time to connect and subscribe before publishing.
    time.sleep(0.3)

    try:
        yield upstream, downstream, up_rep, down_rep
    finally:
        for svc in reversed(downstream.services):
            svc.stop()
        for svc in reversed(upstream.services):
            svc.stop()


def test_event_published_upstream_is_forwarded_downstream(two_nodes):
    upstream, downstream, _up_rep, _down_rep = two_nodes
    received = []
    downstream.bus.subscribe("demo.event", lambda _t, payload: received.append(payload))

    upstream.bus.publish("demo.event", {"value": 42})

    assert _wait_until(lambda: len(received) == 1)
    assert received[0] == {"value": 42}


def test_rpc_call_reaches_registered_handler(two_nodes):
    _upstream, downstream, _up_rep, down_rep = two_nodes

    def _handle_double(payload: dict) -> dict:
        return {"ok": True, "result": payload.get("n", 0) * 2}

    downstream.register_rpc("double", _handle_double)

    client = IPCClient(down_rep, timeout_ms=2000)
    reply = client.call({"cmd": "double", "n": 21})

    assert reply == {"ok": True, "result": 42}


def test_rpc_ping_works_out_of_the_box(two_nodes):
    _upstream, _downstream, up_rep, _down_rep = two_nodes
    client = IPCClient(up_rep)
    assert client.ping() is True


def test_status_reports_only_local_services_not_forwarded_upstream_events(two_nodes):
    upstream, _downstream, _up_rep, down_rep = two_nodes

    upstream.bus.publish("service.started", {"name": "ghost"})

    assert _wait_until(lambda: True, timeout_s=0.2)
    client = IPCClient(down_rep, timeout_ms=2000)
    reply = client.call({"cmd": "status"})

    assert reply["ok"] is True
    services = reply["status"]["services"]
    assert "ghost" not in services
    assert "ipc_bridge" in services


def test_ipc_client_times_out_cleanly_when_nothing_listening():
    client = IPCClient("ipc:///tmp/test-nobody-here.rep", timeout_ms=200)
    reply = client.call({"cmd": "ping"})
    assert reply["ok"] is False
    assert "timeout" in reply["error"]
