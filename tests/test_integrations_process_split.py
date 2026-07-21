"""Integration tests for Phase 2a of the process-isolation proposal: the
`integrations` process (TelegramService + NotificationService + ClockService)
running as a real, independent `ProcessNode`.

Unlike `media` (Phase 1), none of these three services is held as a direct
Python object reference by `WebService`, so there's no proxy to test here —
just that events flow correctly across the process boundary in both
directions, and that thermal telemetry (which core's own IPCBridge does NOT
transitively re-forward) reaches `NotificationService` via a direct
subscription to thermal's PUB endpoint.

See docs/architecture/PROCESS_ISOLATION_PROPOSAL.md §6 (Phase 2a).
"""

import time
import uuid

import pytest

pytest.importorskip("zmq")

from src.assistant.integrations_main import build_node
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
    """A real integrations ProcessNode plus fake "core" and "thermal" nodes
    it upstream-subscribes to, mirroring production wiring."""
    # Isolate QuietHours from the real config/quiet_hours.json on this box.
    import src.assistant.integrations_main as _integ_module
    monkeypatch.setattr(_integ_module, "_CONFIG_DIR", tmp_path)

    core_pub, core_rep = _unique_endpoints("core")
    thermal_pub, thermal_rep = _unique_endpoints("thermal")
    integ_pub, integ_rep = _unique_endpoints("integrations")

    core = ProcessNode(name="core", pub_endpoint=core_pub, rep_endpoint=core_rep)
    thermal = ProcessNode(name="thermal", pub_endpoint=thermal_pub, rep_endpoint=thermal_rep)
    node = build_node(
        cfg={
            "telegram": {"enabled": False},
            "notifications": {
                "thermal_alerts": {"enabled": True, "warn_celsius": 75.0,
                                    "critical_celsius": 85.0, "min_interval_min": 0.0},
                "absence_alerts": {"enabled": False},
            },
            "clock_announcements": {"enabled": False},
        },
        pub_endpoint=integ_pub,
        rep_endpoint=integ_rep,
        upstream_endpoints=[core_pub, thermal_pub],
    )

    for svc in core.services:
        svc.start()
    for svc in thermal.services:
        svc.start()
    for svc in node.services:
        svc.start()

    time.sleep(0.3)

    try:
        yield core, thermal, node
    finally:
        for svc in reversed(node.services):
            svc.stop()
        for svc in reversed(thermal.services):
            svc.stop()
        for svc in reversed(core.services):
            svc.stop()


def test_thermal_temp_direct_subscription_triggers_alert(integrations_node):
    """Proves NotificationService receives thermal.temp via a direct
    subscription to thermal's PUB — NOT transitively through core, since
    core's own IPCBridge deliberately does not re-forward upstream-sourced
    events (that guard is what prevents forwarding loops)."""
    _core, thermal, node = integrations_node

    said = []
    node.bus.subscribe("av.say", lambda _t, payload: said.append(payload))

    thermal.bus.publish("thermal.temp", {"celsius": 90.0, "fan_duty": 100})

    assert _wait_until(lambda: len(said) == 1)
    assert "degrees" in said[0]["text"]


def test_alert_triggered_by_upstream_event_still_escapes_to_own_pub():
    """Regression test: a handler reacting to an event *forwarded in from
    upstream* (thermal.temp) synchronously publishes a brand-new topic
    (av.say) on the same thread as the SUB-loop's injection. That new topic
    must still reach this process's own ZMQ PUB so a third process (core)
    can see it — the anti-echo-loop guard in IPCBridge._forward_to_pub must
    only suppress the exact injected topic (thermal.temp), not every topic
    published while handling it (av.say). This is exactly the shape that
    broke in production for Phase 2a: NotificationService/ClockService
    reacting to an upstream-forwarded event and calling bus.publish("av.say",
    ...), which never reached core's AVService before the fix."""
    thermal_pub, thermal_rep = _unique_endpoints("thermal")
    integ_pub, integ_rep = _unique_endpoints("integrations")
    core_pub, core_rep = _unique_endpoints("core")

    thermal = ProcessNode(name="thermal", pub_endpoint=thermal_pub, rep_endpoint=thermal_rep)
    node = build_node(
        cfg={
            "telegram": {"enabled": False},
            "notifications": {
                "thermal_alerts": {"enabled": True, "warn_celsius": 75.0,
                                    "critical_celsius": 85.0, "min_interval_min": 0.0},
                "absence_alerts": {"enabled": False},
            },
            "clock_announcements": {"enabled": False},
        },
        pub_endpoint=integ_pub,
        rep_endpoint=integ_rep,
        upstream_endpoints=[thermal_pub],
    )
    # Core subscribes to integrations' PUB as an upstream, exactly like
    # core_main.py does via _INTEGRATIONS_PUB.
    core = ProcessNode(
        name="core", pub_endpoint=core_pub, rep_endpoint=core_rep,
        upstream_endpoints=[integ_pub],
    )

    for svc in thermal.services:
        svc.start()
    for svc in node.services:
        svc.start()
    for svc in core.services:
        svc.start()
    time.sleep(0.3)

    try:
        received = []
        core.bus.subscribe("av.say", lambda _t, payload: received.append(payload))

        # Publishing on thermal's bus is what the SUB loop in `node` (the
        # integrations process) will inject — this is the "upstream event"
        # whose handling must not suppress the *new* av.say topic it causes.
        thermal.bus.publish("thermal.temp", {"celsius": 90.0, "fan_duty": 100})

        assert _wait_until(lambda: len(received) == 1)
        assert "degrees" in received[0]["text"]
    finally:
        for svc in reversed(core.services):
            svc.stop()
        for svc in reversed(node.services):
            svc.stop()
        for svc in reversed(thermal.services):
            svc.stop()


def test_core_originated_event_reaches_telegram_service(integrations_node):
    """face.greeted is published on core's local bus (by FaceService, which
    stays in core); proves it's forwarded to the integrations process where
    TelegramService's subscription would normally pick it up."""
    core, _thermal, node = integrations_node

    received = []
    node.bus.subscribe("face.greeted", lambda _t, payload: received.append(payload))

    core.bus.publish("face.greeted", {"face_id": "abc", "name": "Ada", "text": "Hi Ada!"})

    assert _wait_until(lambda: len(received) == 1)
    assert received[0]["name"] == "Ada"


def test_av_say_from_integrations_reaches_core():
    """Proves the reverse direction: av.say published inside the
    integrations process (e.g. a thermal alert) is forwarded to core, where
    AVService (which stays in core) would speak it — symmetric to how
    media.state_changed already reaches core today."""
    core_pub, core_rep = _unique_endpoints("core")
    integ_pub, integ_rep = _unique_endpoints("integrations")

    # Core subscribes to integrations' PUB as an upstream, exactly like
    # core_main.py does via _INTEGRATIONS_PUB.
    core = ProcessNode(
        name="core", pub_endpoint=core_pub, rep_endpoint=core_rep,
        upstream_endpoints=[integ_pub],
    )
    node = build_node(
        cfg={"telegram": {"enabled": False}},
        pub_endpoint=integ_pub,
        rep_endpoint=integ_rep,
        upstream_endpoints=[core_pub],
    )

    for svc in core.services:
        svc.start()
    for svc in node.services:
        svc.start()
    time.sleep(0.3)

    try:
        received = []
        core.bus.subscribe("av.say", lambda _t, payload: received.append(payload))

        node.bus.publish("av.say", {"text": "It is now 3 o'clock."})

        assert _wait_until(lambda: len(received) == 1)
        assert received[0]["text"] == "It is now 3 o'clock."
    finally:
        for svc in reversed(node.services):
            svc.stop()
        for svc in reversed(core.services):
            svc.stop()


def test_quiet_hours_updated_syncs_local_instance(integrations_node):
    """settings.quiet_hours_updated (published by WebService/QuietHoursSkill,
    both still in core) must update this process's independent QuietHours
    instance so NotificationService/ClockService gate on the same state —
    they no longer share the actual object across the process boundary."""
    core, _thermal, node = integrations_node

    # Find the NotificationService instance to inspect its _qh directly.
    notif = next(s for s in node.services if getattr(s, "name", "") == "notification")
    assert notif._qh.enabled is False

    core.bus.publish("settings.quiet_hours_updated", {
        "enabled": True, "start": "22:00", "end": "07:00",
    })

    assert _wait_until(lambda: notif._qh.enabled is True)
    assert notif._qh.start == "22:00"
    assert notif._qh.end == "07:00"
