"""IoTRegistry — holds all registered IoT device plugins."""

from __future__ import annotations

import logging
from typing import Iterator

from src.iot.base import IoTDevice

log = logging.getLogger(__name__)


class IoTRegistry:
    """Registry of all active IoT device plugins.

    Usage::

        registry = IoTRegistry()
        registry.register(MyDevice(bus=bus, cfg=cfg))

        device = registry.get("my_device_id")
        all_devices = registry.all()
        all_snapshots = registry.get_all_snapshots()
    """

    def __init__(self) -> None:
        self._devices: dict[str, IoTDevice] = {}

    # ── Registration ─────────────────────────────────────────────────────────

    def register(self, device: IoTDevice) -> None:
        """Register an IoT device plugin.

        Raises ``ValueError`` if device_id is empty or already registered.
        """
        if not device.device_id:
            raise ValueError(f"IoTDevice subclass {type(device).__name__!r} must set device_id")
        if device.device_id in self._devices:
            raise ValueError(f"IoT device {device.device_id!r} is already registered")
        self._devices[device.device_id] = device
        log.info("IoTRegistry: registered device %r (%s)", device.device_id, device.device_name)

    def unregister(self, device_id: str) -> None:
        """Remove a device from the registry (stops it first if running)."""
        dev = self._devices.pop(device_id, None)
        if dev is None:
            log.warning("IoTRegistry: unregister called for unknown id %r", device_id)
            return
        try:
            dev.stop()
        except Exception:
            log.exception("IoTRegistry: error stopping device %r during unregister", device_id)

    # ── Lookup ───────────────────────────────────────────────────────────────

    def get(self, device_id: str) -> IoTDevice | None:
        """Return the device for *device_id*, or ``None`` if not registered."""
        return self._devices.get(device_id)

    def all(self) -> list[IoTDevice]:
        """Return all registered devices in insertion order."""
        return list(self._devices.values())

    def __iter__(self) -> Iterator[IoTDevice]:
        return iter(self._devices.values())

    def __len__(self) -> int:
        return len(self._devices)

    # ── Bulk data ────────────────────────────────────────────────────────────

    def get_all_snapshots(self) -> dict[str, dict]:
        """Return a mapping of device_id → snapshot dict for all devices."""
        result: dict[str, dict] = {}
        for device_id, device in self._devices.items():
            try:
                snap = device.get_snapshot()
                snap["device_id"]   = device.device_id
                snap["device_name"] = device.device_name
                snap["device_icon"] = device.device_icon
                result[device_id] = snap
            except Exception:
                log.exception("IoTRegistry: error fetching snapshot for %r", device_id)
                result[device_id] = IoTDevice._snapshot_unavailable("snapshot error")
        return result

    def get_device_list(self) -> list[dict]:
        """Return a summary list of all devices (id, name, icon, available)."""
        out = []
        for dev in self._devices.values():
            try:
                snap = dev.get_snapshot()
                available = snap.get("available", False)
            except Exception:
                available = False
            out.append({
                "device_id":   dev.device_id,
                "device_name": dev.device_name,
                "device_icon": dev.device_icon,
                "available":   available,
            })
        return out
