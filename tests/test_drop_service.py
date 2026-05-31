"""Tests for DropService."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from src.services.drop_service import DropService, _Device


# ── Unit tests: service instantiation ────────────────────────────────────────


def test_drop_service_init_defaults():
    svc = DropService()
    assert svc.name == "drop"
    assert svc._mqtt_host == "localhost"
    assert svc._mqtt_port == 1883
    assert svc._alert_on_leak is True
    assert svc._alert_on_salt is True
    assert svc.degraded is False


def test_drop_service_init_custom_cfg():
    cfg = {
        "mqtt_host": "192.168.1.100",
        "mqtt_port": 1884,
        "mqtt_user": "user",
        "mqtt_pass": "pass",
        "alert_on_leak": False,
        "alert_on_salt_low": False,
        "salt_alert_cooldown_s": 3600,
    }
    svc = DropService(cfg=cfg)
    assert svc._mqtt_host == "192.168.1.100"
    assert svc._mqtt_port == 1884
    assert svc._mqtt_user == "user"
    assert svc._alert_on_leak is False
    assert svc._alert_on_salt is False
    assert svc._salt_cooldown == 3600.0


def test_get_reading_initially_none():
    svc = DropService()
    assert svc.get_reading() is None


def test_get_devices_initially_empty():
    svc = DropService()
    assert svc.get_devices() == []


# ── Unit tests: degraded mode when no dependencies ────────────────────────────


def test_start_degraded_no_paho():
    bus = MagicMock()
    with patch("src.services.drop_service._PAHO_OK", False):
        svc = DropService(bus=bus)
        svc.start()
        assert svc.degraded is True
        bus.publish.assert_called_with("service.started", pytest.approx({"name": "drop", "ts": pytest.approx(time.time(), abs=5)}))


def test_start_degraded_no_dropmqttapi():
    bus = MagicMock()
    with patch("src.services.drop_service._DROP_API_OK", False):
        svc = DropService(bus=bus)
        svc.start()
        assert svc.degraded is True


# ── Unit tests: discovery parsing ────────────────────────────────────────────


def test_handle_data_merges_softener_reading():
    """Verify _merge_reading() populates reading fields from a softener device."""
    from dropmqttapi.mqttapi import DropAPI

    svc = DropService()
    api = DropAPI()
    api.parse_drop_message(
        "drop_connect/DROP-123/data/1/state",
        json.dumps({
            "curFlow": 1.5,
            "usedToday": 47.2,
            "capacity": 1500.0,
            "psi": 65.0,
            "psiHigh": 72,
            "psiLow": 58,
            "tdsIn": 320,
            "tdsOut": 12,
            "salt": 0,
            "water": 1,
            "bypass": 0,
            "pMode": "home",
        }).encode(),
        0,
        False,
    )
    device = _Device(name="Softener", dev_type="soft",
                     data_topic="drop_connect/DROP-123/data/1/#")
    device.api = api

    svc._merge_reading(device)
    r = svc.get_reading()
    assert r is not None
    assert r["flow_gpm"] == pytest.approx(1.5)
    assert r["used_today_gal"] == pytest.approx(47.2)
    assert r["capacity_remaining_gal"] == pytest.approx(1500.0)
    assert r["pressure_psi"] == pytest.approx(65.0)
    assert r["tds_in_ppm"] == 320
    assert r["tds_out_ppm"] == 12
    assert r["salt_low"] is False
    assert r["water_on"] is True
    assert r["bypass_on"] is False
    assert r["protect_mode"] == "home"
    assert r["softener_name"] == "Softener"


def test_salt_low_alert_published():
    """_check_alerts() should publish av.say and cooldown properly."""
    from dropmqttapi.mqttapi import DropAPI

    bus = MagicMock()
    svc = DropService(bus=bus, cfg={"alert_on_salt_low": True, "salt_alert_cooldown_s": 0})

    api = DropAPI()
    api.parse_drop_message(
        "drop_connect/DROP-123/data/1/state",
        json.dumps({"salt": 1}).encode(),
        0, False,
    )
    device = _Device(name="Softener", dev_type="soft",
                     data_topic="drop_connect/DROP-123/data/1/#")
    device.api = api

    svc._check_alerts(device)
    bus.publish.assert_called_once()
    topic, payload = bus.publish.call_args[0]
    assert topic == "av.say"
    assert "salt" in payload["text"].lower()


def test_leak_alert_published_once():
    """Leak alert fires on first detection, not on subsequent frames."""
    from dropmqttapi.mqttapi import DropAPI

    bus = MagicMock()
    svc = DropService(bus=bus, cfg={"alert_on_leak": True})

    api = DropAPI()
    api.parse_drop_message(
        "drop_connect/DROP-123/data/2/state",
        json.dumps({"leak": 1}).encode(),
        0, False,
    )
    device = _Device(name="Leak Sensor", dev_type="leak",
                     data_topic="drop_connect/DROP-123/data/2/#")
    device.api = api

    # First detection — alert should fire
    svc._check_alerts(device)
    assert bus.publish.call_count == 2  # av.say + water.drop.leak

    # Second call with same state — should NOT fire again
    bus.reset_mock()
    svc._check_alerts(device)
    assert bus.publish.call_count == 0


def test_no_leak_alert_when_disabled():
    from dropmqttapi.mqttapi import DropAPI

    bus = MagicMock()
    svc = DropService(bus=bus, cfg={"alert_on_leak": False})

    api = DropAPI()
    api.parse_drop_message(
        "drop_connect/DROP-123/data/2/state",
        json.dumps({"leak": 1}).encode(),
        0, False,
    )
    device = _Device(name="Leak Sensor", dev_type="leak",
                     data_topic="drop_connect/DROP-123/data/2/#")
    device.api = api

    svc._check_alerts(device)
    bus.publish.assert_not_called()


def test_auto_register_device_softener():
    """_auto_register_device() creates a softener device from a payload with pMode."""
    svc = DropService()
    import json
    payload = json.dumps({
        "curFlow": 0, "peakFlow": 2.2, "usedToday": 3.37, "avgUsed": 94,
        "water": 1, "bypass": 0, "pMode": "home", "battery": 100,
    }).encode()
    device = svc._auto_register_device("drop_connect/DROP-4_4FE2E2/data/255", payload)
    assert device is not None
    assert device.dev_type == "soft"
    assert device.name == "Softener"
    assert len(svc.get_devices()) == 1


def test_auto_register_device_hub():
    """_auto_register_device() creates a hub device from a payload with capacity."""
    svc = DropService()
    import json
    payload = json.dumps({
        "curFlow": 0, "bypass": 0, "battery": 0, "capacity": 534.33,
        "resInUse": 0, "psi": None,
    }).encode()
    device = svc._auto_register_device("drop_connect/DROP-4_4FE2E2/data/0", payload)
    assert device is not None
    assert device.dev_type == "hub"
    assert device.name == "Hub"
