"""IoT service — manages IoT plugin lifecycle and snapshots for the web UI."""

from __future__ import annotations

import logging

from src.core.service import Service
from src.iot.history_store import IoTHistoryStore
from src.iot.loader import load_persisted, save_persisted
from src.iot.registry import IoTRegistry

log = logging.getLogger(__name__)


class IoTService(Service):
    """Starts/stops IoT plugins and provides a shared registry."""

    name = "iot"
    tick_seconds = 0.0  # event-driven; devices run their own polling threads

    def __init__(self, bus=None, cfg: dict | None = None) -> None:
        super().__init__(bus=bus)
        self._cfg = cfg or {}
        self._history_store = IoTHistoryStore()
        self._registry = IoTRegistry(history_store=self._history_store)

    @property
    def registry(self) -> IoTRegistry:
        return self._registry

    def on_start(self) -> None:
        from src.iot.devices.drop_device import DropDevice
        from src.iot.devices.radon_device import RadonDevice

        # Always-on local devices.
        hardwired = (
            (DropDevice, dict(self._cfg.get("drop", {}))),
            (RadonDevice, dict(self._cfg.get("radon", {}))),
        )
        for cls, cfg in hardwired:
            try:
                dev = cls(bus=self.bus, cfg=cfg)
                self._registry.register(dev)
                dev.start()
            except Exception:
                log.exception("IoTService: failed to start hardwired device %s", cls.__name__)

        # User-managed plugins from config/iot_devices.json.
        try:
            loaded = load_persisted(self._registry, bus=self.bus)
            log.info("IoTService: loaded %d persisted IoT device(s)", loaded)
        except Exception:
            log.exception("IoTService: failed loading persisted IoT devices")

    def on_stop(self) -> None:
        for dev in self._registry.all():
            try:
                dev.stop()
            except Exception:
                log.exception("IoTService: failed stopping device %s", dev.device_id)
        try:
            save_persisted(self._registry)
        except Exception:
            log.exception("IoTService: failed saving IoT registry")
        try:
            self._history_store.save()
        except Exception:
            log.exception("IoTService: failed saving IoT history")
