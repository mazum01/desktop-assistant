"""
VERA IoT plugin system.

To create a new IoT device plugin:

1. Subclass IoTDevice in src/iot/devices/my_device.py:

    from src.iot.base import IoTDevice

    class MyDevice(IoTDevice):
        device_id   = "my_device"
        device_name = "My Device"
        device_icon = "🌡️"

        def start(self): ...
        def stop(self): ...
        def get_snapshot(self) -> dict: ...

2. Register it in src/assistant/core_main.py before WebService is created:

    from src.iot.devices.my_device import MyDevice
    iot_registry.register(MyDevice(bus=bus, cfg=_cfg.get("my_device", {})))

3. Add config in config/assistant.yaml under a matching key.

The web service automatically exposes:
  GET  /api/iot               — list all registered devices
  GET  /api/iot/{id}          — snapshot for a specific device
  POST /api/iot/{id}/announce — speak status via TTS
  POST /api/iot/{id}/action    — execute a device action
  PUT  /api/iot/{id}           — update device config
  POST /api/iot                — add a device
  DELETE /api/iot/{id}         — remove a device

The frontend auto-renders a card in the Smart Home tab for every registered device.
"""

from src.iot.base import IoTDevice
from src.iot.registry import IoTRegistry

__all__ = ["IoTDevice", "IoTRegistry"]
