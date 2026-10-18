"""
ESP32 display status service.

Publishes human-readable startup/restart status updates and optionally writes
them to an ESP32 over BLE.

Topics subscribed:
    display.video           {action, request_id, path?, fps?, loop?, offset?}
    display.set_mouth_state {"state": str}   — set mouth emotion (see MOUTH_STATES)
    av.speaking_started     {"text", "ts"}   — switch mouth to "speaking"
    av.spoke                {"text", "ts"}   — TTS finished; switch mouth back to "neutral"
    system.startup_status, service.started, service.stopped,
    av.version_announced, thermal.error, vision.error, audio.error,
    perception.error        — surfaced as status text on the display

Topics published:
    display.reply, display.reply.<request_id> — correlated device responses/errors
    display.device_event    — asynchronous firmware events (finished/decode errors)
    display.error           — validation, transport, or device failures

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
    # Spoken announcements when the BLE display link comes up or drops.
    announce_connection: bool = True
    connect_text: str = "Display connected."
    disconnect_text: str = "Display disconnected."


class DisplayService(Service):
    name = "display"
    tick_seconds = 0

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        config: Optional[DisplayServiceConfig] = None,
        quiet_hours=None,
    ) -> None:
        super().__init__(bus=bus)
        self._cfg = config or DisplayServiceConfig()
        self._quiet_hours = quiet_hours
        self._unsubs: list = []
        self._seen_started: set[str] = set()
        self._ble_lock = threading.Lock()
        self._ble_warned = False
        self._ble_queue: "queue.Queue[bytes | None]" = queue.Queue(maxsize=128)
        self._ble_worker: Optional[threading.Thread] = None
        self._ble_stop = threading.Event()
        self._speaking = False
        self._last_spectrum_sent = 0.0
        self._notification_buffer = bytearray()
        # None = no link state observed yet, so the first successful connect
        # after boot announces rather than being treated as a no-op transition.
        self._ble_connected: Optional[bool] = None

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
            self.bus.subscribe("display.video", self._on_video),
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

    def _on_video(self, _topic, payload) -> None:
        request_id = payload.get("request_id") if isinstance(payload, dict) else None
        error = None
        if not isinstance(payload, dict) or payload.get("action") not in ("play", "stop", "list", "status"):
            error = "invalid_video_action"
        elif (not isinstance(request_id, str) or not 1 <= len(request_id) <= 64):
            error = "video_request_id_required"
        elif (not self._cfg.ble_enabled or not self._cfg.ble_address
              or not self._cfg.ble_characteristic_uuid or not self._cfg.ble_status_characteristic_uuid):
            error = "ble_display_not_configured"
        elif payload["action"] == "play":
            path = payload.get("path")
            fps = payload.get("fps", 10)
            if (not isinstance(path, str) or not path.startswith("/videos/")
                    or len(path.encode("utf-8")) >= 96 or ".." in path or "\\" in path
                    or "/" in path[8:] or not path.endswith((".mjpg", ".mjpeg"))):
                error = "use_/videos/name.mjpg_or_mjpeg"
            elif type(fps) is not int or not 1 <= fps <= 20 or type(payload.get("loop", False)) is not bool:
                error = "invalid_video_fps_or_loop"
        elif payload["action"] == "list" and (
                type(payload.get("offset", 0)) is not int or not 0 <= payload.get("offset", 0) <= 100000):
            error = "invalid_video_list_offset"
        if error:
            reply = {"cmd": "video", "ok": False, "error": error, "request_id": request_id}
            self._publish_reply(reply)
            self.bus.publish("display.error", reply)
            return
        fields = {key: payload[key] for key in ("action", "path", "fps", "loop", "offset", "request_id")
                  if key in payload}
        encoded = (json.dumps({"cmd": "video", **fields}, separators=(",", ":")) + "\n").encode()
        try:
            self._ble_queue.put_nowait(encoded)
        except queue.Full:
            reply = {"cmd": "video", "ok": False, "error": "display_queue_full", "request_id": request_id}
            self._publish_reply(reply)
            self.bus.publish("display.error", reply)

    def _publish_reply(self, reply: dict) -> None:
        self.bus.publish("display.reply", reply)
        request_id = reply.get("request_id")
        if isinstance(request_id, str) and 1 <= len(request_id) <= 64:
            self.bus.publish(f"display.reply.{request_id}", reply)

    def _on_ble_notification(self, _characteristic, data: bytearray) -> None:
        self._notification_buffer.extend(data)
        if len(self._notification_buffer) > 8192:
            self._notification_buffer.clear()
            self.bus.publish("display.error", {"error": "oversized_display_reply"})
            return
        while b"\n" in self._notification_buffer:
            line, _, rest = self._notification_buffer.partition(b"\n")
            self._notification_buffer[:] = rest
            try:
                reply = json.loads(line)
                if not isinstance(reply, dict):
                    raise ValueError("Display reply must be an object")
            except (ValueError, UnicodeDecodeError):
                log.warning("Invalid BLE display reply", exc_info=True)
                self.bus.publish("display.error", {"error": "invalid_display_reply"})
                continue
            if reply.get("cmd") == "video":
                self._publish_reply(reply)
            elif reply.get("event"):
                self.bus.publish("display.device_event", reply)
            if reply.get("ok") is False:
                self.bus.publish("display.error", reply)

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

    def _set_ble_connected(self, connected: bool) -> None:
        """Record a BLE link transition, announcing and publishing it once.

        Called from the BLE worker thread on every connect/drop. Repeated
        reconnect attempts against an absent display would otherwise announce
        on every retry, so only genuine state changes are reported.
        """
        with self._ble_lock:
            if self._ble_connected == connected:
                return
            first_observation = self._ble_connected is None
            self._ble_connected = connected

        # A failed first connect (display never present) isn't a "disconnect".
        if first_observation and not connected:
            return

        self.bus.publish(
            "display.connection",
            {"connected": connected, "address": self._cfg.ble_address, "ts": time.time()},
        )

        if not self._cfg.announce_connection:
            return
        # Tearing down the link during shutdown is expected, not an event.
        if not connected and self._ble_stop.is_set():
            return
        if self._quiet_hours is not None and self._quiet_hours.is_quiet():
            log.info("Display %s announcement suppressed — quiet hours",
                     "connected" if connected else "disconnected")
            return

        text = self._cfg.connect_text if connected else self._cfg.disconnect_text
        if text:
            self.bus.publish("av.say", {"text": text})

    def _send_ble(self, payload: dict) -> None:
        if not self._cfg.ble_enabled or self._ble_stop.is_set():
            return
        line = json.dumps(payload, separators=(",", ":"), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")
        try:
            self._ble_queue.put_nowait(encoded)
        except queue.Full:
            try:
                dropped = self._ble_queue.get_nowait()
            except queue.Empty:
                dropped = None
            if dropped is not None:
                command = json.loads(dropped)
                if command.get("cmd") == "video":
                    reply = {"cmd": "video", "ok": False, "error": "display_queue_full",
                             "request_id": command.get("request_id")}
                    self._publish_reply(reply)
                    self.bus.publish("display.error", reply)
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
                    self._notification_buffer.clear()
                    if self._cfg.ble_status_characteristic_uuid:
                        await client.start_notify(self._cfg.ble_status_characteristic_uuid,
                                                  self._on_ble_notification)
                    self._set_ble_connected(True)
                    try:
                        while not self._ble_stop.is_set() and client.is_connected:
                            try:
                                encoded = self._ble_queue.get(timeout=0.05)
                            except queue.Empty:
                                await asyncio.sleep(0.01)
                                continue

                            if encoded is None or self._ble_stop.is_set():
                                return

                            try:
                                for start in range(0, len(encoded), 20):
                                    await client.write_gatt_char(
                                        self._cfg.ble_characteristic_uuid,
                                        encoded[start:start + 20],
                                        response=True,
                                    )
                            except Exception:
                                log.exception("DisplayService BLE write failed")
                                self.bus.publish("display.error", {"error": "ble_write_failed", "ts": time.time()})
                                break
                    finally:
                        self._set_ble_connected(False)
            except Exception as exc:
                self._set_ble_connected(False)
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
