"""
ESP32 display status service.

Publishes human-readable startup/restart status updates and optionally writes
them to an ESP32 over BLE.

Topics subscribed:
    display.set_mouth_state {"state": str}   — set mouth emotion (see MOUTH_STATES)
    av.speaking_started     {"text", "ts"}   — switch mouth to "speaking"
    av.spoke                {"text", "ts"}   — TTS finished; switch mouth back to "neutral"
    system.startup_status, service.started, service.stopped,
    av.version_announced, thermal.error, vision.error, audio.error,
    perception.error        — surfaced as status text on the display

The speaking/neutral mouth transitions are automatic and best-effort: they
are skipped when the BLE display is disabled, and any other explicit
display.set_mouth_state request (CLI/voice/skills) simply overrides whatever
state is currently shown.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
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


MOUTH_STATES = ("neutral", "listening", "speaking", "happy", "sad", "surprised", "error")


@dataclass
class DisplayServiceConfig:
    enabled: bool = True
    ble_enabled: bool = False
    ble_address: str = ""
    ble_characteristic_uuid: str = ""
    ble_status_characteristic_uuid: str = ""
    connect_timeout_s: float = 4.0
    max_message_chars: int = 96
    expected_services: list[str] = field(default_factory=list)
    # Real-time graphic-EQ visualization of music/podcast playback.
    spectrum_enabled: bool = True
    # BLE write throughput, not the display, is the limiting factor here, so
    # frames are dropped rather than queued above this rate.
    spectrum_max_fps: float = 12.0
    spectrum_max_bands: int = 12


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
        self._ble_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=128)
        self._ble_worker: Optional[threading.Thread] = None
        self._ble_stop = threading.Event()
        self._speaking = False
        self._last_spectrum_sent = 0.0

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
            self.bus.subscribe("display.set_mouth_state", self._on_set_mouth_state),
            self.bus.subscribe("display.spectrum", self._on_spectrum),
            self.bus.subscribe("av.speaking_started", self._on_speaking_started),
            self.bus.subscribe("av.spoke", self._on_spoke),
        ]
        self._emit_status("boot", "Display status service online")
        if self._cfg.ble_enabled:
            self._ble_stop.clear()
            self._ble_worker = threading.Thread(
                target=self._ble_loop,
                name="display-ble",
                daemon=True,
            )
            self._ble_worker.start()

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs = []
        self._ble_stop.set()
        if self._ble_worker is not None:
            try:
                self._ble_queue.put_nowait(None)
            except queue.Full:
                pass
            self._ble_worker.join(timeout=1.0)
        self._ble_worker = None

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

    def _on_set_mouth_state(self, _topic, payload) -> None:
        state = ""
        if isinstance(payload, dict):
            state = str(payload.get("state", "")).strip()
        elif payload:
            state = str(payload).strip()
        self.set_mouth_state(state)

    def _on_speaking_started(self, _topic, _payload) -> None:
        """Switch the mouth display to 'speaking' while TTS audio is playing.

        Speech outranks the music/podcast spectrum visualization: the mouth
        command itself clears the bars on the device, and the ``_speaking``
        flag suppresses further spectrum frames so an in-flight EQ update
        can't paint over the talking animation mid-sentence.
        """
        self._speaking = True
        if self._cfg.ble_enabled:
            self.set_mouth_state("speaking")

    def _on_spoke(self, _topic, _payload) -> None:
        """Return the mouth display to 'neutral' once TTS playback finishes.

        If music/podcast playback is still running, the next spectrum frame
        re-takes the display on its own, so there's nothing extra to do here.
        """
        self._speaking = False
        if self._cfg.ble_enabled:
            self.set_mouth_state("neutral")

    def _on_spectrum(self, _topic, payload) -> None:
        """Forward a playback spectrum frame to the device as an 'eq' command.

        Frames are dropped (not queued) while speaking, and rate-limited to
        the configured cadence: BLE writes are the bottleneck here, and a
        backed-up queue would render stale spectrum data seconds late.
        """
        if not self._cfg.ble_enabled or not self._cfg.spectrum_enabled:
            return
        if self._speaking:
            return
        if not isinstance(payload, dict):
            return
        bins = payload.get("bins")
        if not isinstance(bins, (list, tuple)) or not bins:
            return

        now = time.monotonic()
        min_interval = 1.0 / max(1.0, float(self._cfg.spectrum_max_fps))
        if now - self._last_spectrum_sent < min_interval:
            return
        self._last_spectrum_sent = now

        try:
            values = [round(max(0.0, min(1.0, float(v))), 3) for v in bins]
        except (TypeError, ValueError):
            return
        max_bands = max(1, int(self._cfg.spectrum_max_bands))
        if len(values) > max_bands:
            values = values[:max_bands]

        self.send_command("eq", bins=values)

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

        self.send_command("status", state=payload["state"], message=text)

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

    def send_command(self, cmd: str, **fields) -> None:
        """Queue a framed JSON command for the ESP32 display firmware.

        Matches the wire protocol in firmware/vera_display/ble_protocol.h:
        one newline-terminated JSON object per message, e.g.
        {"cmd": "mouth", "state": "listening"}.
        """
        message = {"cmd": cmd, **fields}
        self._send_ble(message)

    def send_mouth_state(self, state: str) -> None:
        """Convenience wrapper for {"cmd": "mouth", "state": state}.

        Valid states match firmware/vera_display/mouth_renderer.h:
        neutral, listening, speaking, happy, sad, surprised, error.
        This performs no validation/error-reporting; prefer
        set_mouth_state() for host-driven commands (e.g. CLI/bus/voice).
        """
        self.send_command("mouth", state=str(state).strip())

    def set_mouth_state(self, state: str) -> bool:
        """Validate and send a mouth emotion state, reporting clear errors.

        Returns True if the command was queued for BLE delivery, False if
        rejected (unknown state) or undeliverable (BLE disabled/unconfigured).
        A display.error event is published in the False case so CLI/voice
        callers and other services can surface why it failed.
        """
        normalized = str(state).strip().lower()
        if normalized not in MOUTH_STATES:
            message = (
                f"Unknown mouth state '{state}'; expected one of: "
                f"{', '.join(MOUTH_STATES)}"
            )
            log.warning("%s", message)
            self.bus.publish(
                "display.error", {"error": "unknown_mouth_state", "message": message, "ts": time.time()}
            )
            return False

        if not self._cfg.ble_enabled:
            message = "BLE display disabled; cannot set mouth state"
            log.warning("%s", message)
            self.bus.publish(
                "display.error", {"error": "ble_disabled", "message": message, "ts": time.time()}
            )
            return False

        if not self._cfg.ble_address or not self._cfg.ble_characteristic_uuid:
            self._warn_ble_once("BLE display enabled without address/characteristic; skipping send")
            return False

        self.send_mouth_state(normalized)
        return True

    def _send_ble(self, payload: dict) -> None:
        if not self._cfg.ble_enabled or self._ble_stop.is_set():
            return
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        try:
            self._ble_queue.put_nowait(encoded)
        except queue.Full:
            try:
                _ = self._ble_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._ble_queue.put_nowait(encoded)
            except queue.Full:
                pass

    def _ble_loop(self) -> None:
        try:
            asyncio.run(self._ble_worker_async())
        except Exception:
            log.exception("DisplayService BLE worker loop failed")

    async def _interruptible_sleep(self, seconds: float) -> None:
        steps = max(1, int(seconds / 0.1))
        for _ in range(steps):
            if self._ble_stop.is_set():
                break
            await asyncio.sleep(0.1)

    async def _ble_worker_async(self) -> None:
        while not self._ble_stop.is_set():
            if not self._cfg.ble_address or not self._cfg.ble_characteristic_uuid:
                self._warn_ble_once("BLE display enabled without address/characteristic; skipping send")
                await self._interruptible_sleep(1.0)
                continue
            if not _BLEAK_AVAILABLE or BleakClient is None:
                self._warn_ble_once("bleak not installed; BLE display transport disabled")
                await self._interruptible_sleep(1.0)
                continue

            try:
                log.info("DisplayService: connecting to BLE display (%s)...", self._cfg.ble_address)
                async with BleakClient(
                    self._cfg.ble_address,
                    timeout=float(self._cfg.connect_timeout_s),
                ) as client:
                    log.info("DisplayService: connected to BLE display (%s)", self._cfg.ble_address)
                    while not self._ble_stop.is_set() and client.is_connected:
                        try:
                            encoded = self._ble_queue.get(timeout=0.05)
                        except queue.Empty:
                            await asyncio.sleep(0.01)
                            continue

                        if encoded is None or self._ble_stop.is_set():
                            return

                        try:
                            await client.write_gatt_char(
                                self._cfg.ble_characteristic_uuid,
                                encoded,
                                response=False,
                            )
                        except Exception:
                            log.exception("DisplayService BLE write failed")
                            self.bus.publish("display.error", {"error": "ble_write_failed", "ts": time.time()})
                            break
            except Exception as exc:
                if not self._ble_stop.is_set():
                    log.warning("DisplayService BLE connection error: %s (retrying in 2s)", exc)
                    if "was not found" in str(exc):
                        # BlueZ can be left thinking it still holds a connection
                        # from a previous process (e.g. after an unclean daemon
                        # restart), which makes bleak's scan-based connect fail
                        # with BleakDeviceNotFoundError forever since the device
                        # never shows up as a fresh discoverable peripheral.
                        # Force-clear that stale state via BlueZ's D-Bus API so
                        # the next connect attempt can succeed.
                        await self._force_clear_stale_connection()
                    await self._interruptible_sleep(2.0)

    async def _force_clear_stale_connection(self) -> None:
        address = self._cfg.ble_address
        if not address:
            return
        object_path = f"/org/bluez/hci0/dev_{address.replace(':', '_')}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "gdbus", "call", "--system",
                "--dest", "org.bluez",
                "--object-path", object_path,
                "--method", "org.bluez.Device1.Disconnect",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5.0)
            log.info("DisplayService: cleared stale BlueZ connection state for %s", address)
        except Exception:
            log.debug("DisplayService: could not clear stale BlueZ state for %s", address)

    def _warn_ble_once(self, message: str) -> None:
        if self._ble_warned:
            return
        self._ble_warned = True
        log.warning("%s", message)
        self.bus.publish("display.error", {"error": message, "ts": time.time()})
