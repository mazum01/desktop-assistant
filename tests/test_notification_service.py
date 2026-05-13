"""Tests for NotificationService."""
from unittest.mock import MagicMock, patch
import time

import pytest

from src.services.notification_service import NotificationService


def _make_service(**kwargs):
    bus = MagicMock()
    svc = NotificationService(bus=bus, **kwargs)
    return svc, bus


def test_service_instantiates():
    svc, _ = _make_service()
    assert svc.name == "notification"


def test_thermal_critical_alert_publishes_say():
    svc, bus = _make_service(
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
        critical_celsius=85.0,
        thermal_rate_limit_min=0,
    )
    # Simulate a thermal update above critical threshold
    svc._on_thermal("thermal.temp", {"celsius": 90.0, "fan_duty": 100.0})
    bus.publish.assert_called()
    args = bus.publish.call_args[0]
    assert args[0] == "av.say"


def test_thermal_warn_alert_publishes_say():
    svc, bus = _make_service(
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
        critical_celsius=85.0,
        thermal_rate_limit_min=0,
    )
    svc._on_thermal("thermal.temp", {"celsius": 80.0, "fan_duty": 80.0})
    bus.publish.assert_called()
    args = bus.publish.call_args[0]
    assert args[0] == "av.say"


def test_thermal_alert_suppressed_by_quiet_hours():
    qh = MagicMock()
    qh.is_quiet_now.return_value = True
    svc, bus = _make_service(
        quiet_hours=qh,
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
        thermal_rate_limit_min=0,
    )
    svc._on_thermal("thermal.temp", {"celsius": 80.0, "fan_duty": 80.0})
    bus.publish.assert_not_called()


def test_thermal_alert_suppressed_while_speaking():
    svc, bus = _make_service(
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
        thermal_rate_limit_min=0,
    )
    svc._speaking = True
    svc._on_thermal("thermal.temp", {"celsius": 80.0, "fan_duty": 80.0})
    bus.publish.assert_not_called()


def test_thermal_alert_rate_limited():
    svc, bus = _make_service(
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
        thermal_rate_limit_min=60,  # 60 minute rate limit
    )
    # Fire once — should notify
    svc._on_thermal("thermal.temp", {"celsius": 80.0, "fan_duty": 80.0})
    first_count = bus.publish.call_count

    # Fire again immediately — should be rate-limited
    svc._on_thermal("thermal.temp", {"celsius": 80.0, "fan_duty": 80.0})
    assert bus.publish.call_count == first_count


def test_no_alert_below_threshold():
    svc, bus = _make_service(
        thermal_alerts_enabled=True,
        warn_celsius=75.0,
    )
    svc._on_thermal("thermal.temp", {"celsius": 60.0, "fan_duty": 30.0})
    bus.publish.assert_not_called()


def test_face_seen_resets_last_seen():
    svc, _ = _make_service()
    before = svc._last_face_seen
    time.sleep(0.01)
    svc._on_faces("perception.faces", {"count": 1})
    assert svc._last_face_seen > before
