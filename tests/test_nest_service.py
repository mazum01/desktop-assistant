"""Tests for NestService."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from src.services.nest_service import NestService, _c_to_f, _f_to_c


# ── Temperature conversion helpers ────────────────────────────────────────────

def test_c_to_f_freezing():
    assert _c_to_f(0.0) == 32.0


def test_c_to_f_boiling():
    assert _c_to_f(100.0) == 212.0


def test_f_to_c_freezing():
    assert _f_to_c(32.0) == 0.0


def test_f_to_c_body_temp():
    assert abs(_f_to_c(98.6) - 37.0) < 0.05


def test_round_trip():
    for f in (32.0, 68.0, 72.0, 98.6):
        assert abs(_c_to_f(_f_to_c(f)) - f) < 0.1


# ── NestService degraded when unconfigured ─────────────────────────────────

def test_degraded_missing_config():
    svc = NestService(cfg={})
    assert svc.degraded
    assert "not configured" in svc._degraded_reason.lower()


def test_degraded_partial_config():
    svc = NestService(cfg={"project_id": "proj", "client_id": "cid"})
    assert svc.degraded


def test_not_degraded_when_full_config():
    cfg = {
        "project_id":    "proj-123",
        "client_id":     "client-abc",
        "client_secret": "secret-xyz",
        "refresh_token": "refresh-tok",
    }
    svc = NestService(cfg=cfg)
    assert not svc.degraded


# ── get_reading returns None before first poll ─────────────────────────────

def test_get_reading_initially_none():
    cfg = {
        "project_id":    "p",
        "client_id":     "c",
        "client_secret": "s",
        "refresh_token": "r",
    }
    svc = NestService(cfg=cfg)
    assert svc.get_reading() is None


# ── _parse_traits ─────────────────────────────────────────────────────────

def test_parse_traits_full():
    svc = NestService(cfg={
        "project_id": "p", "client_id": "c",
        "client_secret": "s", "refresh_token": "r",
    })
    traits = {
        "sdm.devices.traits.Temperature":             {"ambientTemperatureCelsius": 21.0},
        "sdm.devices.traits.Humidity":                {"ambientHumidityPercent": 45},
        "sdm.devices.traits.ThermostatMode":          {"mode": "HEAT"},
        "sdm.devices.traits.ThermostatHvac":          {"status": "HEATING"},
        "sdm.devices.traits.ThermostatTemperatureSetpoint": {"heatCelsius": 22.0},
    }
    r = svc._parse_traits(traits)
    assert r["temp_c"] == 21.0
    assert r["temp_f"] == pytest.approx(69.8, abs=0.1)
    assert r["humidity"] == 45
    assert r["mode"] == "HEAT"
    assert r["hvac_status"] == "HEATING"
    assert r["heat_c"] == 22.0
    assert r["heat_f"] == pytest.approx(71.6, abs=0.1)


def test_parse_traits_eco():
    svc = NestService(cfg={
        "project_id": "p", "client_id": "c",
        "client_secret": "s", "refresh_token": "r",
    })
    traits = {
        "sdm.devices.traits.ThermostatMode": {"mode": "MANUAL_ECO"},
        "sdm.devices.traits.ThermostatEco":  {
            "mode": "MANUAL_ECO",
            "heatCelsius": 18.0,
            "coolCelsius": 26.0,
        },
    }
    r = svc._parse_traits(traits)
    assert r["eco_mode"] == "MANUAL_ECO"
    assert r["eco_heat_f"] == pytest.approx(64.4, abs=0.1)
    assert r["eco_cool_f"] == pytest.approx(78.8, abs=0.1)


# ── start / stop with degraded service does not raise ─────────────────────

def test_start_stop_degraded():
    svc = NestService(cfg={})
    svc.start()   # should be a no-op
    svc.stop()    # should not raise


# ── Token refresh ─────────────────────────────────────────────────────────

def test_ensure_access_token_uses_cached():
    cfg = {
        "project_id":    "p",
        "client_id":     "c",
        "client_secret": "s",
        "refresh_token": "r",
    }
    svc = NestService(cfg=cfg)
    svc._access_token = "cached-tok"
    svc._token_expires_at = time.time() + 3600
    with patch("urllib.request.urlopen") as mock_open:
        result = svc._ensure_access_token()
    assert result is True
    mock_open.assert_not_called()   # should NOT refresh since token is fresh


def test_ensure_access_token_refreshes_expired():
    cfg = {
        "project_id":    "p",
        "client_id":     "c",
        "client_secret": "s",
        "refresh_token": "r",
    }
    svc = NestService(cfg=cfg)
    svc._token_expires_at = 0.0   # force expiry

    response_data = json.dumps({"access_token": "new-tok", "expires_in": 3600}).encode()
    mock_resp = MagicMock()
    mock_resp.__enter__ = MagicMock(return_value=mock_resp)
    mock_resp.__exit__ = MagicMock(return_value=False)
    mock_resp.read = MagicMock(return_value=response_data)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = svc._ensure_access_token()

    assert result is True
    assert svc._access_token == "new-tok"
