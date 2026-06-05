"""Google Nest thermostat service via the Smart Device Management (SDM) API.

Polls the thermostat state every ``poll_interval`` seconds and exposes
``get_reading()`` for consumption by NestDevice.  Supports set_heat,
set_cool, and set_mode actions.

Setup (one-time):
  1. Create a project at console.nest.google.com ($5 one-time fee).
  2. Enable the SDM API in Google Cloud; create an OAuth2 client ID/secret.
  3. Authorise via browser and obtain a refresh_token:
       vera iot action nest_thermostat auth
     (or manually via: https://developers.google.com/nest/device-access/authorize)
  4. Add the device via:
       vera iot add nest_thermostat

Config keys (passed via ``cfg`` dict):
    project_id      Google Device Access project ID (required).
    client_id       Google Cloud OAuth2 client ID (required).
    client_secret   Google Cloud OAuth2 client secret (required).
    refresh_token   OAuth2 refresh token (required).
    device_id       Specific SDM device name (optional; uses first thermostat).
    poll_interval   Seconds between polls (default: 60).

All temperatures are stored internally in Celsius and converted to
Fahrenheit only for display.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

_SDM_BASE = "https://smartdevicemanagement.googleapis.com/v1"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_POLL_DEFAULT = 60
_TOKEN_REFRESH_MARGIN_S = 120   # Refresh access token this many seconds early


def _c_to_f(c: float) -> float:
    return round(c * 9.0 / 5.0 + 32.0, 1)


def _f_to_c(f: float) -> float:
    return round((f - 32.0) * 5.0 / 9.0, 2)


class NestService:
    """Background-polling adapter for the Google SDM (Nest) thermostat API."""

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        self.bus = bus
        self._cfg = cfg or {}
        self.degraded = False
        self._degraded_reason = ""

        self._project_id: str = self._cfg.get("project_id", "").strip()
        self._client_id: str = self._cfg.get("client_id", "").strip()
        self._client_secret: str = self._cfg.get("client_secret", "").strip()
        self._refresh_token: str = self._cfg.get("refresh_token", "").strip()
        self._target_device_id: str = self._cfg.get("device_id", "").strip()
        self._poll_interval: int = int(self._cfg.get("poll_interval", _POLL_DEFAULT))

        self._access_token: str = ""
        self._token_expires_at: float = 0.0
        self._token_lock = threading.Lock()

        self._reading: dict | None = None
        self._reading_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        if not all([self._project_id, self._client_id, self._client_secret, self._refresh_token]):
            self.degraded = True
            self._degraded_reason = (
                "Nest not configured. Provide project_id, client_id, client_secret, "
                "and refresh_token via 'vera iot config nest_thermostat …'."
            )
            log.warning("NestService: %s", self._degraded_reason)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self.degraded:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, name="nest-poll", daemon=True)
        self._thread.start()
        log.info("NestService: started (poll every %ds)", self._poll_interval)

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        log.info("NestService: stopped")

    # ── Public data API ───────────────────────────────────────────────────────

    def get_reading(self) -> dict | None:
        with self._reading_lock:
            return dict(self._reading) if self._reading else None

    # ── Public action API ─────────────────────────────────────────────────────

    def set_heat(self, temp_f: float) -> tuple[bool, str]:
        temp_c = _f_to_c(temp_f)
        return self._execute_command(
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat",
            {"heatCelsius": temp_c},
        )

    def set_cool(self, temp_f: float) -> tuple[bool, str]:
        temp_c = _f_to_c(temp_f)
        return self._execute_command(
            "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool",
            {"coolCelsius": temp_c},
        )

    def set_mode(self, mode: str) -> tuple[bool, str]:
        """Set HVAC mode. mode: HEAT, COOL, HEATCOOL, OFF, or MANUAL_ECO."""
        if mode == "ECO":
            return self._execute_command(
                "sdm.devices.commands.ThermostatEco.SetMode",
                {"mode": "MANUAL_ECO"},
            )
        return self._execute_command(
            "sdm.devices.commands.ThermostatMode.SetMode",
            {"mode": mode},
        )

    def run_fan(self, minutes: int) -> tuple[bool, str]:
        """Run the fan for a given number of minutes (1–43200)."""
        minutes = max(1, min(43200, int(minutes)))
        return self._execute_command(
            "sdm.devices.commands.Fan.SetTimer",
            {"timerMode": "ON", "duration": f"{minutes * 60}s"},
        )

    # ── Internal polling ──────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._do_poll()
            except Exception:
                log.exception("NestService: poll failed")
            self._stop_event.wait(self._poll_interval)

    def _do_poll(self) -> None:
        device_name = self._get_device_name()
        if not device_name:
            return
        data = self._sdm_get(f"{_SDM_BASE}/{device_name}")
        if data is None:
            return
        traits = data.get("traits", {})
        reading = self._parse_traits(traits)
        raw_name = data.get("displayName") or data.get("name", "")
        # SDM resource paths look like "enterprises/.../devices/..."; use custom
        # name from traits if available, otherwise fall back to "Thermostat".
        if raw_name and "/" not in raw_name:
            display = raw_name
        else:
            display = reading.get("custom_name") or "Thermostat"
        reading["device_name"] = display
        with self._reading_lock:
            self._reading = reading
        log.debug("NestService: poll ok — %s", reading)

    def _parse_traits(self, traits: dict) -> dict:
        temp_c = None
        humidity = None
        mode = "UNKNOWN"
        available_modes: list[str] = []
        hvac_status = "OFF"
        heat_c: float | None = None
        cool_c: float | None = None
        eco_mode = "OFF"
        eco_heat_c: float | None = None
        eco_cool_c: float | None = None
        connectivity = "ONLINE"
        fan_mode = "OFF"
        custom_name = ""

        t = traits.get("sdm.devices.traits.Temperature", {})
        if "ambientTemperatureCelsius" in t:
            temp_c = float(t["ambientTemperatureCelsius"])

        h = traits.get("sdm.devices.traits.Humidity", {})
        if "ambientHumidityPercent" in h:
            humidity = round(float(h["ambientHumidityPercent"]))

        m = traits.get("sdm.devices.traits.ThermostatMode", {})
        mode = m.get("mode", "UNKNOWN")
        available_modes = m.get("availableModes", [])

        hv = traits.get("sdm.devices.traits.ThermostatHvac", {})
        hvac_status = hv.get("status", "OFF")

        sp = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        if "heatCelsius" in sp:
            heat_c = float(sp["heatCelsius"])
        if "coolCelsius" in sp:
            cool_c = float(sp["coolCelsius"])

        eco = traits.get("sdm.devices.traits.ThermostatEco", {})
        eco_mode = eco.get("mode", "OFF")
        if "heatCelsius" in eco:
            eco_heat_c = float(eco["heatCelsius"])
        if "coolCelsius" in eco:
            eco_cool_c = float(eco["coolCelsius"])

        conn = traits.get("sdm.devices.traits.Connectivity", {})
        connectivity = conn.get("status", "ONLINE")

        fan = traits.get("sdm.devices.traits.Fan", {})
        fan_mode = fan.get("timerMode", "OFF")

        info = traits.get("sdm.devices.traits.Info", {})
        custom_name = info.get("customName", "")

        return {
            "temp_c": temp_c,
            "temp_f": _c_to_f(temp_c) if temp_c is not None else None,
            "humidity": humidity,
            "mode": mode,
            "available_modes": available_modes,
            "hvac_status": hvac_status,
            "heat_c": heat_c,
            "heat_f": _c_to_f(heat_c) if heat_c is not None else None,
            "cool_c": cool_c,
            "cool_f": _c_to_f(cool_c) if cool_c is not None else None,
            "eco_mode": eco_mode,
            "eco_heat_f": _c_to_f(eco_heat_c) if eco_heat_c is not None else None,
            "eco_cool_f": _c_to_f(eco_cool_c) if eco_cool_c is not None else None,
            "connectivity": connectivity,
            "fan_mode": fan_mode,
            "custom_name": custom_name,
        }

    # ── SDM helpers ───────────────────────────────────────────────────────────

    def _get_device_name(self) -> str | None:
        if self._target_device_id:
            return self._target_device_id
        data = self._sdm_get(
            f"{_SDM_BASE}/enterprises/{self._project_id}/devices"
        )
        if data is None:
            return None
        for dev in data.get("devices", []):
            traits = dev.get("traits", {})
            if "sdm.devices.traits.ThermostatMode" in traits:
                name = dev.get("name", "")
                log.info("NestService: auto-selected thermostat %s", name)
                return name
        log.warning("NestService: no thermostat found in device list")
        return None

    def _execute_command(self, command: str, params: dict) -> tuple[bool, str]:
        device_name = self._target_device_id or self._discover_device_name()
        if not device_name:
            return False, "Could not determine Nest device ID"
        url = f"{_SDM_BASE}/{device_name}:executeCommand"
        body = {"command": command, "params": params}
        result = self._sdm_post(url, body)
        if result is None:
            return False, "Command failed — check logs"
        self._stop_event.clear()  # trigger immediate repoll on next cycle
        threading.Thread(target=self._do_poll, daemon=True, name="nest-repoll").start()
        return True, "OK"

    def _discover_device_name(self) -> str | None:
        if self._reading:
            return None  # can't derive name from reading; require explicit device_id
        return self._get_device_name()

    # ── OAuth2 / HTTP ─────────────────────────────────────────────────────────

    def _ensure_access_token(self) -> bool:
        with self._token_lock:
            if time.time() < self._token_expires_at - _TOKEN_REFRESH_MARGIN_S:
                return True
            payload = urllib.parse.urlencode({  # type: ignore[attr-defined]
                "client_id":     self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": self._refresh_token,
                "grant_type":    "refresh_token",
            }).encode()
            req = urllib.request.Request(
                _TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                self._access_token = data["access_token"]
                self._token_expires_at = time.time() + int(data.get("expires_in", 3600))
                log.debug("NestService: access token refreshed")
                return True
            except Exception as exc:
                log.warning("NestService: token refresh failed: %s", exc)
                self.degraded = True
                self._degraded_reason = f"OAuth2 token refresh failed: {exc}"
                return False

    def _sdm_get(self, url: str) -> dict | None:
        if not self._ensure_access_token():
            return None
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self._access_token}"},
            method="GET",
        )
        return self._do_request(req)

    def _sdm_post(self, url: str, body: dict) -> dict | None:
        if not self._ensure_access_token():
            return None
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._access_token}",
                "Content-Type":  "application/json",
            },
            method="POST",
        )
        return self._do_request(req)

    @staticmethod
    def _do_request(req: urllib.request.Request) -> dict | None:
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                return json.loads(raw.decode()) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            log.warning("NestService: HTTP %d: %s", exc.code, body[:300])
        except Exception as exc:
            log.warning("NestService: request error: %s", exc)
        return None
