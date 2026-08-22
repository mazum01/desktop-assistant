"""
ESP32 display status service.

Publishes human-readable startup/restart status updates and optionally writes
them to an ESP32 over BLE.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

try:
    from bleak import BleakClient  # type: ignore
    _BLEAK_AVAILABLE = True
except Exception:  # pragma: no cover - environment-dependent import
    BleakClient = None  # type: ignore
    _BLEAK_AVAILABLE = False


@dataclass
class DisplayServiceConfig:
    enabled: bool = True
    ble_enabled: bool = False
    ble_address: str = ""
    ble_characteristic_uuid: str = ""
    connect_timeout_s: float = 4.0
    max_message_chars: int = 96
    expected_services: list[str] = field(default_factory=list)


class DisplayService(Service):
    name = "display"
    tick_seconds = 0

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[DisplayServiceConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._cfg = config or DisplayServiceConfig()
        self._unsubs: list = []
        self._seen_started: set[str] = set()
        self._ble_lock = threading.Lock()
        self._ble_warned = False

    def set_expected_services(self, names: list[str]) -> None:
        self._cfg.expected_services = [n for n in names if n and n != self.name]

    def on_start(self) -> None:
        self._unsubs = [
            self.bus.subscribe("system.startup_status", self._on_startup_status),
            self.bus.subscribe("service.started", self._on_service_started),
            self.bus.subscribe("service.stopped", self._on_service_stopped),
            self.bus.subscribe("av.version_announced", self._on_version_announced),
            self.bus.subscribe("thermal.error", self._on_thermal_error),
            self.bus.subscribe("vision.error", self._on_vision_error),
            self.bus.subscribe("audio.error", self._on_audio_error),
            self.bus.subscribe("perception.error", self._on_perception_error),
        ]
        self._emit_status("boot", "Display status service online")

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs = []

    # ── Handlers ────────────────────────────────────────────────────────

    def _on_startup_status(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        state = str(payload.get("state", "startup"))
        msg = str(payload.get("message", "")).strip()
        if msg:
            self._emit_status(state, msg)

    def _on_service_started(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        name = str(payload.get("name", "")).strip()
        if not name or name == self.name:
            return
        self._seen_started.add(name)
        self._emit_status("service_started", f"{name} ready")

        expected = set(self._cfg.expected_services)
        if expected and expected.issubset(self._seen_started):
            self._emit_status("ready", "Desktop assistant ready")

    def _on_service_stopped(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        name = str(payload.get("name", "")).strip()
        if name and name != self.name:
            self._emit_status("service_stopped", f"{name} stopped")

    def _on_version_announced(self, _topic, payload) -> None:
        version = ""
        if isinstance(payload, dict):
            version = str(payload.get("version", "")).strip()
        if version:
            self._emit_status("version", f"VERA v{version}")

    def _on_thermal_error(self, _topic, payload) -> None:
        self._emit_status("degraded", f"Thermal issue: {self._payload_text(payload)}")

    def _on_vision_error(self, _topic, payload) -> None:
        self._emit_status("degraded", f"Vision issue: {self._payload_text(payload)}")

    def _on_audio_error(self, _topic, payload) -> None:
        self._emit_status("degraded", f"Audio issue: {self._payload_text(payload)}")

    def _on_perception_error(self, _topic, payload) -> None:
        self._emit_status("degraded", f"Perception issue: {self._payload_text(payload)}")

    # ── Emission / BLE transport ────────────────────────────────────────

    def _emit_status(self, state: str, message: str) -> None:
        text = " ".join(str(message).split())
        if not text:
            return
        max_chars = max(8, int(self._cfg.max_message_chars))
        if len(text) > max_chars:
            text = text[: max_chars - 1].rstrip() + "…"

        payload = {
            "state": str(state).strip() or "status",
            "message": text,
            "ts": time.time(),
        }
        self.bus.publish("display.status", payload)

        if self._cfg.enabled:
            self._send_ble(payload)

    @staticmethod
    def _payload_text(payload) -> str:
        if payload is None:
            return "unknown"
        if isinstance(payload, dict):
            if "error" in payload:
                return str(payload.get("error"))
            if "message" in payload:
                return str(payload.get("message"))
        return str(payload)

    def _send_ble(self, payload: dict) -> None:
        if not self._cfg.ble_enabled:
            return
        if not self._cfg.ble_address or not self._cfg.ble_characteristic_uuid:
            self._warn_ble_once("BLE display enabled without address/characteristic; skipping send")
            return
        if not _BLEAK_AVAILABLE:
            self._warn_ble_once("bleak not installed; BLE display transport disabled")
            return

        encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        with self._ble_lock:
            try:
                asyncio.run(self._ble_write(encoded))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    loop.run_until_complete(self._ble_write(encoded))
                finally:
                    loop.close()
            except Exception:
                log.exception("DisplayService BLE write failed")
                self.bus.publish("display.error", {"error": "ble_write_failed", "ts": time.time()})

    async def _ble_write(self, encoded: bytes) -> None:
        async with BleakClient(  # type: ignore[misc]
            self._cfg.ble_address,
            timeout=float(self._cfg.connect_timeout_s),
        ) as client:
            await client.write_gatt_char(
                self._cfg.ble_characteristic_uuid,
                encoded,
                response=False,
            )

    def _warn_ble_once(self, message: str) -> None:
        if self._ble_warned:
            return
        self._ble_warned = True
        log.warning("%s", message)
        self.bus.publish("display.error", {"error": message, "ts": time.time()})
