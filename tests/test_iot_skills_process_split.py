"""Integration tests for Phase 2b of the process-isolation proposal: the
`IoTService` and `SkillsService` RPC handlers running inside the real
`integrations` process, exercised through `IoTRegistryProxy`/
`SkillsServiceProxy` (`src/core/integrations_client.py`) exactly as
`WebService` uses them.

Unlike `test_integrations_process_split.py` (Phase 2a), these two services
ARE held as direct object references by `WebService`, so the interesting
thing to test is the request/reply round trip through real ZeroMQ sockets —
not just bus-event forwarding.

See docs/architecture/PROCESS_ISOLATION_PROPOSAL.md §6 (Phase 2b).
"""

from __future__ import annotations

import time
import uuid

import pytest

pytest.importorskip("zmq")

from src.assistant.integrations_main import build_node
from src.core.integrations_client import IoTRegistryProxy, SkillsServiceProxy
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
def integrations_node(tmp_path, monkeypatch):
    """A real integrations ProcessNode (IoTService + SkillsService included)
    plus a fake "core" node it upstream-subscribes to, mirroring production
    wiring — and proxies bound to its REP endpoint, mirroring what
    `core_main.py` builds for `WebService`."""
    # Isolate QuietHours from the real config/quiet_hours.json on this box.
    import src.assistant.integrations_main as _integ_module
    monkeypatch.setattr(_integ_module, "_CONFIG_DIR", tmp_path)

    # Isolate IoT persistence from the real config/iot_devices.json and
    # ~/.local/share/desktop-assistant/iot_history.json on this box.
    import src.iot.loader as _iot_loader
    import src.iot.history_store as _iot_history_store
    monkeypatch.setattr(_iot_loader, "_PERSIST_PATH", tmp_path / "iot_devices.json")
    monkeypatch.setattr(_iot_history_store, "_DEFAULT_PATH", tmp_path / "iot_history.json")

    core_pub, core_rep = _unique_endpoints("core")
    integ_pub, integ_rep = _unique_endpoints("integrations")

    node = build_node(
        cfg={
            "telegram": {"enabled": False},
            "clock_announcements": {"enabled": False},
            # No drop/radon credentials — both run in degraded mode, which
            # is fine; we only care about IoT plugin CRUD via the loader,
            # not the two hardwired devices' real connectivity.
        },
        pub_endpoint=integ_pub,
        rep_endpoint=integ_rep,
        upstream_endpoints=[core_pub],
    )
    # Core subscribes to integrations' PUB as an upstream, exactly like
    # core_main.py does via _INTEGRATIONS_PUB — needed for the
    # SkillsService-dispatches-av.say-back-to-core test below.
    core = ProcessNode(
        name="core", pub_endpoint=core_pub, rep_endpoint=core_rep,
        upstream_endpoints=[integ_pub],
    )

    for svc in core.services:
        svc.start()
    for svc in node.services:
        svc.start()

    time.sleep(0.3)

    client = IPCClient(integ_rep, timeout_ms=2000)
    iot_proxy = IoTRegistryProxy(client)
    skills_proxy = SkillsServiceProxy(client)

    try:
        yield core, node, iot_proxy, skills_proxy
    finally:
        for svc in reversed(node.services):
            svc.stop()
        for svc in reversed(core.services):
            svc.stop()


# ── IoT RPCs ────────────────────────────────────────────────────────────────


def test_iot_snapshots_includes_hardwired_devices(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    snaps = iot_proxy.get_all_snapshots()
    assert "drop" in snaps
    assert "radon" in snaps


def test_iot_list_returns_devices_and_snapshots(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.list_all()
    assert reply["ok"] is True
    device_ids = {d["device_id"] for d in reply["devices"]}
    assert {"drop", "radon"} <= device_ids
    assert "drop" in reply["snapshots"]


def test_iot_get_detail_not_found(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.get_detail("nonexistent_device")
    assert reply["ok"] is False
    assert reply["reason"] == "not_found"


def test_iot_get_detail_returns_full_shape(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.get_detail("radon")
    assert reply["ok"] is True
    assert reply["device_id"] == "radon"
    assert "actions" in reply
    assert "snapshot" in reply
    assert "config" in reply


def test_iot_add_get_action_delete_round_trip(integrations_node):
    """Exercises the full CRUD lifecycle for a persisted (non-hardwired)
    device type through the real RPC boundary."""
    _core, _node, iot_proxy, _skills_proxy = integrations_node

    from src.iot import loader as iot_loader
    known_types = iot_loader.get_type_list()
    non_hardwired = next(
        (t["type_id"] for t in known_types if t["type_id"] not in ("drop", "radon")),
        None,
    )
    assert non_hardwired is not None, "expected at least one non-hardwired IoT type"

    add_reply = iot_proxy.add(non_hardwired, {})
    assert add_reply["ok"] is True
    device_id = add_reply["device_id"]

    detail = iot_proxy.get_detail(device_id)
    assert detail["ok"] is True
    assert detail["device_id"] == device_id

    del_reply = iot_proxy.delete(device_id)
    assert del_reply["ok"] is True

    assert iot_proxy.get_detail(device_id)["reason"] == "not_found"


def test_iot_add_bad_type_id_returns_bad_request(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.add("not_a_real_type", {})
    assert reply["ok"] is False
    assert reply["reason"] == "bad_request"
    assert "error" in reply


def test_iot_action_not_found(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.execute_action("nonexistent_device", "auth", {})
    assert reply["ok"] is False
    assert reply["reason"] == "not_found"


def test_iot_announce_not_found(integrations_node):
    _core, _node, iot_proxy, _skills_proxy = integrations_node
    reply = iot_proxy.announce("nonexistent_device")
    assert reply["ok"] is False
    assert reply["reason"] == "not_found"


# ── Skills RPCs ───────────────────────────────────────────────────────────


def test_skills_list_includes_known_skills(integrations_node):
    _core, _node, _iot_proxy, skills_proxy = integrations_node
    reply = skills_proxy.list_skills()
    names = {s["name"] for s in reply["skills"]}
    assert "help" in names
    assert "tell_time" in names


def test_skills_set_enabled_round_trip(integrations_node):
    _core, _node, _iot_proxy, skills_proxy = integrations_node
    reply = skills_proxy.set_enabled("tell_time", False)
    assert reply["ok"] is True
    assert reply["enabled"] is False

    listing = skills_proxy.list_skills()
    tell_time = next(s for s in listing["skills"] if s["name"] == "tell_time")
    assert tell_time["enabled"] is False

    # Restore, since this is a real shared skill instance across the test.
    skills_proxy.set_enabled("tell_time", True)


def test_skills_set_enabled_not_found(integrations_node):
    _core, _node, _iot_proxy, skills_proxy = integrations_node
    reply = skills_proxy.set_enabled("nonexistent_skill", True)
    assert reply["ok"] is False
    assert reply["reason"] == "not_found"


def test_skills_get_config_not_found(integrations_node):
    _core, _node, _iot_proxy, skills_proxy = integrations_node
    reply = skills_proxy.get_config("nonexistent_skill")
    assert reply["ok"] is False
    assert reply["reason"] == "not_found"


def test_skills_get_and_set_config_round_trip(integrations_node):
    _core, _node, _iot_proxy, skills_proxy = integrations_node
    reply = skills_proxy.get_config("smart_home")
    assert reply["ok"] is True
    assert reply["name"] == "smart_home"
    assert "schema" in reply
    assert "values" in reply

    set_reply = skills_proxy.set_config("smart_home", "base_url", "http://homeassistant.local:8123")
    assert set_reply["ok"] is True
    assert set_reply["value"] == "http://homeassistant.local:8123"


# ── Cross-process bus event: av.utterance dispatch ─────────────────────────


def test_utterance_from_core_dispatched_by_skills_service(integrations_node):
    """av.utterance published on core's bus (by the STT pipeline, CLI, or
    WebService's /api/utterance route, all of which stay in core) must
    reach SkillsService running in the integrations process via the
    upstream subscription, and its dispatch (av.say) must flow back to
    core via the symmetric downstream forwarding."""
    core, _node, _iot_proxy, _skills_proxy = integrations_node

    said = []
    core.bus.subscribe("av.say", lambda _t, payload: said.append(payload))

    core.bus.publish("av.utterance", {"text": "what time is it"})

    assert _wait_until(lambda: len(said) == 1)
