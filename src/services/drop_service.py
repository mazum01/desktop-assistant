"""
DROP water softener service — local MQTT integration.

Connects to a local Mosquitto MQTT broker, listens for DROP Hub device
discovery announcements, subscribes to data topics for each softener,
filter, leak detector, and salt sensor, and caches the latest reading.

The service publishes on VERA's internal bus and exposes a ``get_reading()``
method for the web API.

SETUP (one-time):
    1. Install Mosquitto: ``sudo apt-get install -y mosquitto``
    2. Open the DROP Connect app → System → Advanced → Configure MQTT.
    3. Set broker address to this Pi's IP, port 1883.
    4. Tap Connect — devices will be discovered within ~60 s.

Topics published:
    water.drop.reading  — dict with all softener/device metrics
    water.drop.leak     — {"detected": True, "device": name} on leak

Topics consumed (MQTT, not VERA bus):
    drop_connect/discovery/#          — device discovery
    drop_connect/{hub}/data/{dev}/#   — per-device telemetry

Config keys (all optional):
    mqtt_host              — MQTT broker host (default: localhost)
    mqtt_port              — MQTT broker port (default: 1883)
    mqtt_user              — broker username (default: empty)
    mqtt_pass              — broker password (default: empty)
    alert_on_leak          — speak TTS on leak detection (default: True)
    alert_on_salt_low      — speak TTS when salt is low (default: True)
    salt_alert_cooldown_s  — seconds between repeat salt-low alerts (default: 7200)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# DROP MQTT topic constants (from dropmqttapi / HA integration)
_DISCOVERY_TOPIC = "drop_connect/discovery/#"
_DOMAIN          = "drop_connect"

# Device type constants
_DEV_SOFTENER = "soft"
_DEV_HUB      = "hub"
_DEV_SALT     = "salt"
_DEV_LEAK     = "leak"
_DEV_FILTER   = "filt"
_DEV_PV       = "pv"

# ── Optional imports ─────────────────────────────────────────────────────────
try:
    import paho.mqtt.client as mqtt_client  # type: ignore
    _PAHO_OK = True
except ImportError:
    _PAHO_OK = False
    log.warning("paho-mqtt not installed — DropService degraded. "
                "Run: pip install paho-mqtt dropmqttapi")

try:
    from dropmqttapi.mqttapi    import DropAPI
    from dropmqttapi.discovery  import DropDiscovery
    _DROP_API_OK = True
except ImportError:
    _DROP_API_OK = False
    log.warning("dropmqttapi not installed — DropService degraded. "
                "Run: pip install dropmqttapi")


class _Device:
    """Container for one discovered DROP device."""
    def __init__(self, name: str, dev_type: str, data_topic: str):
        self.name      = name
        self.dev_type  = dev_type
        self.data_topic = data_topic
        self.api       = DropAPI() if _DROP_API_OK else None
        self.last_seen: float = 0.0


class DropService:
    """
    Background service for the DROP local MQTT water system.

    Starts in *degraded mode* if paho-mqtt or dropmqttapi are not installed,
    or if the MQTT broker is unreachable.  All public methods are safe to call
    in degraded mode — they return ``None`` or ``{"available": False}``.
    """

    name = "drop"

    def __init__(self, bus=None, cfg: Optional[dict] = None) -> None:
        self.bus  = bus
        self._cfg = cfg or {}

        self._mqtt_host: str  = str(self._cfg.get("mqtt_host", "localhost"))
        self._mqtt_port: int  = int(self._cfg.get("mqtt_port", 1883))
        self._mqtt_user: str  = str(self._cfg.get("mqtt_user", "") or "")
        self._mqtt_pass: str  = str(self._cfg.get("mqtt_pass", "") or "")

        self._alert_on_leak:   bool  = bool(self._cfg.get("alert_on_leak",   True))
        self._alert_on_salt:   bool  = bool(self._cfg.get("alert_on_salt_low", True))
        self._salt_cooldown:   float = float(self._cfg.get("salt_alert_cooldown_s", 7200))

        self._devices:  dict[str, _Device] = {}  # key = hub_id + "_" + device_id
        self._reading:  dict = {}
        self._lock    = threading.Lock()
        self._running = False
        self._degraded = False

        self._last_salt_alert: float = float("-inf")
        self._last_leak_state: dict[str, bool] = {}

        self._client: Optional[object] = None  # paho client

    # ── Service lifecycle ──────────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running

    def start(self) -> None:
        if not _PAHO_OK or not _DROP_API_OK:
            log.warning("DropService: dependencies missing — starting degraded")
            self._degraded = True
            self._announce_started()
            return

        thread = threading.Thread(target=self._run, daemon=True, name="drop-mqtt")
        thread.start()
        self._announce_started()

    def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                self._client.disconnect()  # type: ignore[attr-defined]
            except Exception:
                pass
        self._announce_stopped()

    # ── Public API ─────────────────────────────────────────────────────────

    @property
    def degraded(self) -> bool:
        return self._degraded

    def get_reading(self) -> Optional[dict]:
        """Return the latest merged snapshot of all DROP device data."""
        with self._lock:
            return dict(self._reading) if self._reading else None

    def get_devices(self) -> list[dict]:
        """Return list of discovered devices with names and types."""
        with self._lock:
            return [
                {"key": k, "name": d.name, "type": d.dev_type, "last_seen": d.last_seen}
                for k, d in self._devices.items()
            ]

    # ── MQTT connection loop ───────────────────────────────────────────────

    def _run(self) -> None:
        self._running = True
        reconnect_delay = 5.0

        while self._running:
            try:
                self._connect_and_loop()
            except Exception as exc:
                log.warning("DropService: MQTT error (%s) — retrying in %ds", exc, reconnect_delay)
                self._degraded = True
                time.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 60)
            else:
                reconnect_delay = 5.0

        self._running = False

    def _connect_and_loop(self) -> None:
        client = mqtt_client.Client(  # type: ignore[attr-defined]
            mqtt_client.CallbackAPIVersion.VERSION2,  # type: ignore[attr-defined]
            client_id="vera-drop-service",
            clean_session=True,
        )
        client.on_connect    = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message    = self._on_message

        if self._mqtt_user:
            client.username_pw_set(self._mqtt_user, self._mqtt_pass or None)

        self._client = client
        client.connect(self._mqtt_host, self._mqtt_port, keepalive=60)
        client.loop_forever()

    # ── MQTT callbacks ─────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        rc = reason_code.value if hasattr(reason_code, "value") else reason_code
        if rc != 0:
            log.warning("DropService: MQTT connect failed rc=%s", rc)
            return
        log.info("DropService: connected to %s:%s", self._mqtt_host, self._mqtt_port)
        self._degraded = False
        client.subscribe(_DISCOVERY_TOPIC, qos=0)
        log.info("DropService: subscribed to %s", _DISCOVERY_TOPIC)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None) -> None:
        log.warning("DropService: disconnected from MQTT broker")
        self._degraded = True

    def _on_message(self, client, userdata, msg) -> None:
        topic   = msg.topic
        payload = msg.payload

        if "/discovery/" in topic:
            self._handle_discovery(client, topic, payload)
        elif "/data/" in topic:
            self._handle_data(topic, payload)

    # ── Discovery handling ─────────────────────────────────────────────────

    def _handle_discovery(self, client, topic: str, payload: bytes) -> None:
        disc = DropDiscovery(_DOMAIN)

        # Run async parse_discovery in a blocking manner
        import asyncio
        loop = asyncio.new_event_loop()
        ok = loop.run_until_complete(disc.parse_discovery(topic, payload))
        loop.close()

        if not ok:
            return

        key = f"{disc.hub_id}_{disc.device_id}"
        with self._lock:
            if key not in self._devices:
                log.info(
                    "DropService: discovered %s '%s' (type=%s, topic=%s)",
                    key, disc.name, disc.device_type, disc.data_topic,
                )
                self._devices[key] = _Device(
                    name=disc.name,
                    dev_type=disc.device_type,
                    data_topic=disc.data_topic,
                )
        # Subscribe to data topic for this device
        client.subscribe(disc.data_topic, qos=0)
        log.debug("DropService: subscribed to data topic %s", disc.data_topic)

    # ── Data handling ──────────────────────────────────────────────────────

    def _handle_data(self, topic: str, payload: bytes) -> None:
        # Find matching device by topic prefix
        device = self._find_device_for_topic(topic)
        if device is None:
            return

        changed = device.api.parse_drop_message(topic, payload, 0, False)
        if not changed:
            return

        device.last_seen = time.time()
        self._merge_reading(device)
        self._check_alerts(device)

        with self._lock:
            snapshot = dict(self._reading)

        if self.bus:
            self.bus.publish("water.drop.reading", snapshot)

    def _find_device_for_topic(self, topic: str) -> Optional[_Device]:
        with self._lock:
            for device in self._devices.values():
                # data_topic is like "drop_connect/DROP-xxx/data/1/#"
                prefix = device.data_topic.rstrip("/#")
                if topic.startswith(prefix):
                    return device
        return None

    def _merge_reading(self, device: _Device) -> None:
        api = device.api
        now = time.time()
        with self._lock:
            if device.dev_type == _DEV_SOFTENER:
                self._reading.update({
                    "softener_name":       device.name,
                    "flow_gpm":            api.current_flow_rate(),
                    "peak_flow_gpm":       api.peak_flow_rate(),
                    "used_today_gal":      api.water_used_today(),
                    "avg_used_gal":        api.average_water_used(),
                    "capacity_remaining_gal": api.capacity_remaining(),
                    "pressure_psi":        api.current_system_pressure(),
                    "pressure_high_psi":   api.high_system_pressure(),
                    "pressure_low_psi":    api.low_system_pressure(),
                    "temp_f":              api.temperature(),
                    "tds_in_ppm":          api.inlet_tds(),
                    "tds_out_ppm":         api.outlet_tds(),
                    "salt_low":            bool(api.salt_low()),
                    "water_on":            api.water() == 1 if api.water() is not None else None,
                    "bypass_on":           api.bypass() == 1 if api.bypass() is not None else None,
                    "protect_mode":        api.protect_mode(),
                    "last_updated":        now,
                })
            elif device.dev_type in (_DEV_LEAK, _DEV_SALT):
                self._reading.update({
                    f"{device.dev_type}_{device.name}_leak":     bool(api.leak_detected()),
                    f"{device.dev_type}_{device.name}_battery":  api.battery(),
                    "last_updated": now,
                })
            elif device.dev_type == _DEV_HUB:
                self._reading.update({
                    "hub_name":      device.name,
                    "hub_pressure":  api.current_system_pressure(),
                    "hub_flow":      api.current_flow_rate(),
                    "last_updated":  now,
                })

    def _check_alerts(self, device: _Device) -> None:
        api = device.api
        now = time.time()

        # Leak alert
        if self._alert_on_leak and api.leak_detected():
            was_leaking = self._last_leak_state.get(device.name, False)
            if not was_leaking:
                self._last_leak_state[device.name] = True
                msg = f"Warning: DROP leak detected at {device.name}! Check for water leaks immediately."
                log.warning("DropService: LEAK detected on %s", device.name)
                if self.bus:
                    self.bus.publish("av.say", {"text": msg})
                    self.bus.publish("water.drop.leak", {"detected": True, "device": device.name})
        else:
            self._last_leak_state[device.name] = False

        # Salt-low alert (softener only, with cooldown)
        if (
            self._alert_on_salt
            and device.dev_type == _DEV_SOFTENER
            and api.salt_low()
            and (now - self._last_salt_alert) > self._salt_cooldown
        ):
            self._last_salt_alert = now
            msg = "Heads up: the water softener salt level is low. Time to add salt to the brine tank."
            log.warning("DropService: salt LOW on %s", device.name)
            if self.bus:
                self.bus.publish("av.say", {"text": msg})

    # ── Bus helpers ────────────────────────────────────────────────────────

    def _announce_started(self) -> None:
        if self.bus:
            self.bus.publish("service.started", {"name": self.name, "ts": time.time()})

    def _announce_stopped(self) -> None:
        if self.bus:
            self.bus.publish("service.stopped", {"name": self.name, "ts": time.time()})
