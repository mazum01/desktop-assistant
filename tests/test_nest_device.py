"""Tests for NestDevice IoT plugin."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.iot.devices.nest_device import NestDevice


# ── Construction ──────────────────────────────────────────────────────────────

def test_device_id():
    dev = NestDevice(cfg={})
    assert dev.device_id == "nest_thermostat"


def test_device_icon():
    dev = NestDevice(cfg={})
    assert dev.device_icon == "🌡️"


def test_device_label():
    dev = NestDevice(cfg={})
    assert "nest" in dev.device_name.lower() or "therm" in dev.device_name.lower()


# ── get_actions ───────────────────────────────────────────────────────────────

def test_get_actions_returns_list():
    dev = NestDevice(cfg={})
    actions = dev.get_actions()
    assert isinstance(actions, list)
    assert len(actions) > 0


def test_set_heat_action_present():
    dev = NestDevice(cfg={})
    ids = [a["id"] for a in dev.get_actions()]
    assert "set_heat" in ids


def test_set_cool_action_present():
    dev = NestDevice(cfg={})
    ids = [a["id"] for a in dev.get_actions()]
    assert "set_cool" in ids


def test_mode_actions_present():
    dev = NestDevice(cfg={})
    ids = [a["id"] for a in dev.get_actions()]
    for expected in ("mode_heat", "mode_cool", "mode_eco", "mode_off"):
        assert expected in ids, f"Missing action: {expected}"


def test_run_fan_action_present():
    dev = NestDevice(cfg={})
    ids = [a["id"] for a in dev.get_actions()]
    assert "run_fan" in ids


def test_run_fan_requires_input():
    dev = NestDevice(cfg={})
    action = next(a for a in dev.get_actions() if a["id"] == "run_fan")
    assert action.get("requires_input") is True
    assert action.get("input_param") == "minutes"


def test_get_actions_filters_by_available_modes():
    dev = NestDevice(cfg={})
    ids = [a["id"] for a in dev.get_actions(available_modes=["HEAT", "OFF"])]
    assert "mode_heat" in ids
    assert "mode_cool" not in ids
    assert "mode_range" not in ids
    assert "mode_off" in ids


def test_set_heat_requires_input():
    dev = NestDevice(cfg={})
    action = next(a for a in dev.get_actions() if a["id"] == "set_heat")
    assert action.get("requires_input") is True
    assert action.get("input_prompt")
    assert action.get("input_param") == "temperature"


def test_set_cool_requires_input():
    dev = NestDevice(cfg={})
    action = next(a for a in dev.get_actions() if a["id"] == "set_cool")
    assert action.get("requires_input") is True
    assert action.get("input_param") == "temperature"


# ── get_snapshot when degraded ────────────────────────────────────────────────

def test_get_snapshot_degraded():
    dev = NestDevice(cfg={})
    snap = dev.get_snapshot()
    assert isinstance(snap, dict)
    # Should still return a dict; check it doesn't raise
    assert "status" in snap or "error" in snap or "degraded" in snap


# ── execute_action when degraded ──────────────────────────────────────────────

def test_execute_action_degraded_returns_error():
    dev = NestDevice(cfg={})
    result = dev.execute_action("set_heat", {"temperature": "72"})
    assert result.get("ok") is False
    assert result.get("message") or result.get("error")


def test_execute_action_auth_returns_url():
    dev = NestDevice(cfg={})
    dev._svc = MagicMock()
    dev._svc.build_auth_url.return_value = "https://accounts.google.com/o/oauth2/v2/auth?..."
    out = dev.execute_action("auth")
    assert out["ok"] is True
    assert "auth_url" in out


def test_execute_action_exchange_code_returns_refresh_token_message():
    dev = NestDevice(cfg={})
    dev._svc = MagicMock()
    dev._svc.exchange_auth_code.return_value = (True, {"refresh_token": "rtok-123"})
    out = dev.execute_action("exchange_code", {"code": "abc"})
    assert out["ok"] is True
    assert "refresh_token" in out
    assert "vera iot config nest_thermostat refresh_token=" in out["message"]


# ── announce ──────────────────────────────────────────────────────────────────

def test_announce_no_data_returns_string():
    dev = NestDevice(cfg={})
    msg = dev.announce()
    assert isinstance(msg, str)
    assert len(msg) > 0


def test_announce_with_data():
    dev = NestDevice(cfg={})
    dev.start()  # starts with degraded service (cfg={})
    if dev._svc:
        dev._svc._reading = {
            "temp_f":     70.0,
            "humidity":   45,
            "mode":       "HEAT",
            "hvac_status": "IDLE",
            "heat_f":     72.0,
        }
        dev._svc.degraded = False
    msg = dev.announce()
    assert isinstance(msg, str)
    assert len(msg) > 0


# ── start / stop ──────────────────────────────────────────────────────────────

def test_start_stop_no_raise():
    dev = NestDevice(cfg={})
    dev.start()
    dev.stop()
