from unittest.mock import MagicMock
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
        items = []
        while not svc._ble_queue.empty():
            items.append(svc._ble_queue.get_nowait())
        assert any(b'"cmd":"mouth"' in item and b'"state":"listening"' in item for item in items)
    finally:
        svc.stop()


def test_send_mouth_state_wraps_send_command():
    bus, svc, _emitted = _start_service(
        ble_enabled=True,
        ble_address="AA:BB:CC:DD:EE:FF",
        ble_characteristic_uuid="d741e8c9-f156-4c47-808f-f28ccd2760f2",
    )
    try:
        svc.send_mouth_state("happy")
        items = []
        while not svc._ble_queue.empty():
            items.append(svc._ble_queue.get_nowait())
        assert any(b'"cmd":"mouth"' in item and b'"state":"happy"' in item for item in items)
    finally:
        svc.stop()


def test_set_mouth_state_via_bus_topic_queues_command():
    bus, svc, _emitted = _start_service(
        ble_enabled=True,
        ble_address="AA:BB:CC:DD:EE:FF",
        ble_characteristic_uuid="d741e8c9-f156-4c47-808f-f28ccd2760f2",
    )
    try:
        bus.publish("display.set_mouth_state", {"state": "speaking"})
        items = []
        while not svc._ble_queue.empty():
            items.append(svc._ble_queue.get_nowait())
        assert any(b'"cmd":"mouth"' in item and b'"state":"speaking"' in item for item in items)
    finally:
        svc.stop()


def test_set_mouth_state_rejects_unknown_state():
    bus, svc, _emitted = _start_service(
        ble_enabled=True,
        ble_address="AA:BB:CC:DD:EE:FF",
        ble_characteristic_uuid="d741e8c9-f156-4c47-808f-f28ccd2760f2",
    )
    errors: list[dict] = []
    bus.subscribe("display.error", lambda _t, payload: errors.append(payload))
    try:
        # Drain the initial boot status from on_start
        while not svc._ble_queue.empty():
            svc._ble_queue.get_nowait()
        assert svc.set_mouth_state("confused") is False
        assert any(e.get("error") == "unknown_mouth_state" for e in errors)
        assert svc._ble_queue.empty()
    finally:
        svc.stop()


def test_set_mouth_state_rejects_when_ble_disabled():
    bus, svc, _emitted = _start_service(ble_enabled=False)
    errors: list[dict] = []
    bus.subscribe("display.error", lambda _t, payload: errors.append(payload))
    try:
        assert svc.set_mouth_state("happy") is False
        assert any(e.get("error") == "ble_disabled" for e in errors)
    finally:
        svc.stop()


# ── Graphic-EQ spectrum routing ─────────────────────────────────────────
#
# Speech must always outrank the music/podcast visualization: an in-flight
# spectrum frame can't be allowed to paint over the talking animation.

def _spectrum_svc():
    from src.services.display_service import DisplayService, DisplayServiceConfig

    svc = DisplayService(
        bus=MagicMock(),
        config=DisplayServiceConfig(ble_enabled=True, spectrum_enabled=True),
    )
    svc.send_command = MagicMock()
    return svc


def test_spectrum_frame_forwarded_as_eq_command():
    svc = _spectrum_svc()
    svc._on_spectrum("display.spectrum", {"bins": [0.1, 0.5, 0.9]})
    svc.send_command.assert_called_once()
    assert svc.send_command.call_args.args[0] == "eq"
    assert svc.send_command.call_args.kwargs["bins"] == [0.1, 0.5, 0.9]


def test_spectrum_suppressed_while_speaking():
    svc = _spectrum_svc()
    svc._on_speaking_started("av.speaking_started", {})
    svc.send_command.reset_mock()
    svc._on_spectrum("display.spectrum", {"bins": [0.4, 0.4]})
    svc.send_command.assert_not_called()


def test_spectrum_resumes_after_speech_finishes():
    svc = _spectrum_svc()
    svc._on_speaking_started("av.speaking_started", {})
    svc._on_spoke("av.spoke", {})
    svc.send_command.reset_mock()
    svc._last_spectrum_sent = 0.0
    svc._on_spectrum("display.spectrum", {"bins": [0.4, 0.4]})
    svc.send_command.assert_called_once()


def test_spectrum_values_clamped_and_truncated():
    svc = _spectrum_svc()
    svc._cfg.spectrum_max_bands = 3
    svc._on_spectrum("display.spectrum", {"bins": [-1.0, 2.0, 0.5, 0.7, 0.9]})
    bins = svc.send_command.call_args.kwargs["bins"]
    assert bins == [0.0, 1.0, 0.5]


def test_spectrum_rate_limited():
    svc = _spectrum_svc()
    svc._cfg.spectrum_max_fps = 1.0
    svc._on_spectrum("display.spectrum", {"bins": [0.5]})
    svc._on_spectrum("display.spectrum", {"bins": [0.6]})
    assert svc.send_command.call_count == 1


def test_spectrum_ignored_when_disabled():
    svc = _spectrum_svc()
    svc._cfg.spectrum_enabled = False
    svc._on_spectrum("display.spectrum", {"bins": [0.5]})
    svc.send_command.assert_not_called()


def test_spectrum_ignores_malformed_payloads():
    svc = _spectrum_svc()
    for bad in (None, {}, {"bins": []}, {"bins": "nope"}, {"bins": ["x"]}):
        svc._last_spectrum_sent = 0.0
        svc._on_spectrum("display.spectrum", bad)
    svc.send_command.assert_not_called()
