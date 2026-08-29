from src.core.bus import MessageBus
from src.services.display_service import DisplayService, DisplayServiceConfig


def _start_service(**overrides):
    bus = MessageBus()
    cfg_kwargs = dict(
        enabled=True,
        ble_enabled=False,
        expected_services=["vision", "av"],
    )
    cfg_kwargs.update(overrides)
    svc = DisplayService(
        bus=bus,
        config=DisplayServiceConfig(**cfg_kwargs),
    )
    emitted: list[dict] = []
    bus.subscribe("display.status", lambda _t, payload: emitted.append(payload))
    svc.start()
    return bus, svc, emitted


def test_display_service_relays_startup_status():
    bus, svc, emitted = _start_service()
    try:
        bus.publish("system.startup_status", {"state": "starting_service", "message": "Starting vision"})
        assert any(e.get("message") == "Starting vision" for e in emitted)
    finally:
        svc.stop()


def test_display_service_emits_ready_when_expected_services_seen():
    bus, svc, emitted = _start_service()
    try:
        bus.publish("service.started", {"name": "vision"})
        bus.publish("service.started", {"name": "av"})
        assert any(e.get("state") == "ready" for e in emitted)
    finally:
        svc.stop()


def test_display_service_emits_degraded_on_error():
    bus, svc, emitted = _start_service()
    try:
        bus.publish("thermal.error", {"error": "sensor offline"})
        assert any(
            e.get("state") == "degraded" and "sensor offline" in e.get("message", "")
            for e in emitted
        )
    finally:
        svc.stop()


def test_send_command_queues_framed_json_when_ble_enabled():
    bus, svc, _emitted = _start_service(
        ble_enabled=True,
        ble_address="AA:BB:CC:DD:EE:FF",
        ble_characteristic_uuid="d741e8c9-f156-4c47-808f-f28ccd2760f2",
    )
    try:
        svc.send_command("mouth", state="listening")
        encoded = svc._ble_queue.get(timeout=1.0)
        assert encoded is not None
        assert encoded.endswith(b"\n")
        assert b'"cmd":"mouth"' in encoded
        assert b'"state":"listening"' in encoded
    finally:
        svc.stop()


def test_send_command_noop_when_ble_disabled():
    bus, svc, _emitted = _start_service(ble_enabled=False)
    try:
        svc.send_command("mouth", state="listening")
        assert svc._ble_queue.empty()
    finally:
        svc.stop()
