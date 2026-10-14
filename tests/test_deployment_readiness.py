"""Readiness reports the loaded process identity and current service health."""

from unittest.mock import MagicMock

from src.core.bus import MessageBus
from src.services.ipc_bridge import IPCBridge


def test_readiness_does_not_read_version_from_disk():
    bridge = IPCBridge(bus=MessageBus())
    bridge._loaded_version = "1.64.0"
    bridge._loaded_revision = "a" * 40
    service = MagicMock()
    service.name = "thermal"
    service.is_running.return_value = True
    service._manager.sensor_ok = True
    service._manager.fan_backend = "sysfs"
    service._manager.fan_duty = 100
    bridge._all_services = [service]
    bridge._received_at = {"thermal.temp": 123.0}
    response = bridge._handle_request(b'{"cmd":"deployment.readiness"}')
    assert response["version"] == "1.64.0"
    assert response["revision"] == "a" * 40
    assert response["services"]["thermal"]["sensor_ok"]
    assert response["received_at"]["thermal.temp"] == 123.0
