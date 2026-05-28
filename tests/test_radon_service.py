"""Tests for RadonService."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.services.radon_service import RadonService, _compute_alert


# ── Unit tests: alert level computation ──────────────────────────────────────


def test_compute_alert_green():
    assert _compute_alert(1.0) == "Green"
    assert _compute_alert(0.0) == "Green"
    assert _compute_alert(2.69) == "Green"


def test_compute_alert_orange():
    assert _compute_alert(2.7) == "Orange"
    assert _compute_alert(3.5) == "Orange"
    assert _compute_alert(3.99) == "Orange"


def test_compute_alert_red():
    assert _compute_alert(4.0) == "Red"
    assert _compute_alert(10.0) == "Red"


# ── Unit tests: RadonService construction ─────────────────────────────────────


def test_radon_service_init_defaults():
    svc = RadonService()
    assert svc.name == "radon"
    assert svc.get_reading() is None
    assert not svc.is_running()


def test_radon_service_degraded_mode_no_credentials(monkeypatch):
    """Service starts in degraded mode when env vars are absent."""
    monkeypatch.delenv("ECOSENSE_USERNAME", raising=False)
    monkeypatch.delenv("ECOSENSE_PASSWORD", raising=False)

    svc = RadonService()
    svc.start()
    try:
        assert svc.is_running()
        assert svc.degraded
        assert svc.get_reading() is None
    finally:
        svc.stop()


def test_radon_service_stop_when_degraded(monkeypatch):
    """stop() is safe to call when the service is in degraded mode."""
    monkeypatch.delenv("ECOSENSE_USERNAME", raising=False)
    monkeypatch.delenv("ECOSENSE_PASSWORD", raising=False)

    svc = RadonService()
    svc.start()
    svc.stop()
    assert not svc.is_running()


# ── Unit tests: _parse_device ─────────────────────────────────────────────────


def test_parse_device_normal():
    svc = RadonService()
    device = {
        "serial_number": "ECQ-001",
        "device_name": "EcoQube - Basement",
        "fw_version": "1.0.0",
        "radon_level": "44.4",   # Bq/m³ — API always returns Bq/m³
    }
    reading = svc._parse_device(device)
    assert reading["radon_bqm3"] == pytest.approx(44.4, abs=0.1)
    assert reading["radon_pcil"] == pytest.approx(44.4 / 37.0, abs=0.01)
    assert reading["alert"] == "Green"
    assert reading["device_name"] == "EcoQube - Basement"
    assert reading["serial_number"] == "ECQ-001"
    assert reading["error"] is None


def test_parse_device_zero_level():
    """Zero radon_level means the device is initialising."""
    svc = RadonService()
    reading = svc._parse_device({"radon_level": "0", "device_name": "EcoQube"})
    assert reading["radon_bqm3"] is None
    assert "initialising" in reading["error"]


def test_parse_device_null_level():
    """None radon_level treated as initialising."""
    svc = RadonService()
    reading = svc._parse_device({"radon_level": None, "device_name": "EcoQube"})
    assert reading["radon_pcil"] is None


def test_parse_device_red_level():
    """EPA Red threshold (> 4.0 pCi/L ≈ > 148 Bq/m³)."""
    svc = RadonService()
    reading = svc._parse_device({"radon_level": "200.0"})
    assert reading["alert"] == "Red"
    assert reading["radon_pcil"] > 4.0


# ── Unit tests: get_reading thread safety ────────────────────────────────────


def test_get_reading_returns_copy():
    """get_reading() must return a copy so callers can't mutate the cache."""
    svc = RadonService()
    device = {"radon_level": "74.0", "device_name": "EcoQube"}
    with svc._lock:
        svc._latest = svc._parse_device(device)
    r1 = svc.get_reading()
    r2 = svc.get_reading()
    assert r1 == r2
    r1["radon_pcil"] = 9999
    assert svc.get_reading()["radon_pcil"] != 9999


# ── Unit tests: Red alert TTS ─────────────────────────────────────────────────


def test_red_alert_publishes_tts():
    bus = MagicMock()
    svc = RadonService(bus=bus, cfg={"alert_on_red": True, "red_alert_cooldown_s": 0})

    red_reading = {
        "radon_pcil": 5.0,
        "radon_bqm3": 185.0,
        "alert": "Red",
        "device_name": "EcoQube",
    }
    svc._maybe_alert_red(red_reading)
    bus.publish.assert_called_once()
    topic, payload = bus.publish.call_args[0]
    assert topic == "tts.speak"
    assert "radon" in payload["text"].lower()


def test_red_alert_respects_cooldown():
    bus = MagicMock()
    svc = RadonService(bus=bus, cfg={"alert_on_red": True, "red_alert_cooldown_s": 3600})

    red_reading = {"radon_pcil": 5.0, "radon_bqm3": 185.0, "alert": "Red"}
    svc._maybe_alert_red(red_reading)
    svc._maybe_alert_red(red_reading)   # second call should be suppressed
    assert bus.publish.call_count == 1


def test_green_alert_does_not_trigger_tts():
    bus = MagicMock()
    svc = RadonService(bus=bus, cfg={"alert_on_red": True})
    green_reading = {"radon_pcil": 1.0, "radon_bqm3": 37.0, "alert": "Green"}
    svc._maybe_alert_red(green_reading)
    bus.publish.assert_not_called()
