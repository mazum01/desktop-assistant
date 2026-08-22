from src.core.bus import MessageBus
from src.services.display_service import DisplayService, DisplayServiceConfig


def _start_service():
    bus = MessageBus()
    svc = DisplayService(
        bus=bus,
        config=DisplayServiceConfig(
            enabled=True,
            ble_enabled=False,
            expected_services=["vision", "av"],
        ),
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
