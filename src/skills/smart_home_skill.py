"""Skill: smart home control via Home Assistant REST API.

This skill is **disabled by default**.  To activate it, set the
``base_url`` and ``token`` config fields from the web GUI or CLI:

    da skills config smart_home base_url=http://homeassistant.local:8123
    da skills config smart_home token=<long-lived access token>
    da skills enable smart_home

Voice examples
--------------
  "turn on the living room lights"
  "turn off the bedroom light"
  "set thermostat to 72"
  "is the front door locked"
  "lock the front door"
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from typing import Any, Optional

from .base import ConfigField, Skill

log = logging.getLogger(__name__)

_TIMEOUT_S = 4


class SmartHomeSkill(Skill):
    """Home Assistant REST API integration."""

    name = "smart_home"

    def __init__(self) -> None:
        super().__init__()
        self.enabled = False  # disabled until configured
        self._base_url: str = ""
        self._token: str = ""
        self._default_room: str = ""

    # ------------------------------------------------------------------
    # Patterns
    # ------------------------------------------------------------------

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bturn (on|off) (?:the )?(.+?)(light|lights|lamp|fan|switch|outlet)\b"),
            re.compile(r"\b(lights?|lamp) (on|off)\b"),
            re.compile(r"\bset (the )?thermostat to (\d+)\b"),
            re.compile(r"\b(heat|cool) (to|it to) (\d+)\b"),
            re.compile(r"\b(lock|unlock) (?:the )?(.+?door|door)\b"),
            re.compile(r"\bis (?:the )?(.+?door|door) (locked|unlocked|open|closed)\b"),
            re.compile(r"\bsmart home\b"),
        ]

    # ------------------------------------------------------------------
    # Handle
    # ------------------------------------------------------------------

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        if not self._base_url or not self._token:
            return (
                "Smart home is not configured. "
                "Set the base URL and token in the skills config panel."
            )

        lower = text.lower()

        # Turn on/off a device
        m_toggle = re.search(
            r"\bturn (on|off) (?:the )?(.+?)(?:\s+(lights?|lamp|fan|switch|outlet))?\s*$",
            lower,
        )
        if m_toggle:
            state  = m_toggle.group(1)     # "on" or "off"
            device = (m_toggle.group(2) or "").strip()
            return self._toggle_device(device, state == "on")

        # Thermostat
        m_temp = re.search(r"\b(?:thermostat|heat|cool).*?(\d+)\b", lower)
        if m_temp:
            temp = int(m_temp.group(1))
            return self._set_thermostat(temp)

        # Lock / unlock
        m_lock = re.search(r"\b(lock|unlock) (?:the )?(.+?)\s*$", lower)
        if m_lock:
            action = m_lock.group(1)
            target = m_lock.group(2).strip()
            return self._lock_device(target, action == "lock")

        return "I'm not sure what smart home action you want. Try 'turn on the living room lights'."

    # ------------------------------------------------------------------
    # HA REST helpers
    # ------------------------------------------------------------------

    def _ha_call(self, method: str, path: str, body: dict | None = None) -> dict | None:
        url = self._base_url.rstrip("/") + path
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type":  "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            log.warning("SmartHomeSkill: HA HTTP %s: %s", exc.code, exc)
        except Exception as exc:
            log.warning("SmartHomeSkill: HA request failed: %s", exc)
        return None

    def _toggle_device(self, device: str, on: bool) -> str:
        # Try to find an entity matching the device description
        service = "turn_on" if on else "turn_off"
        entity_id = self._guess_entity_id(device)
        result = self._ha_call("POST", f"/api/services/light/{service}",
                                {"entity_id": entity_id})
        if result is None:
            return f"Couldn't reach Home Assistant to turn {device} {service.split('_')[1]}."
        return f"Turned {'on' if on else 'off'} {device}."

    def _set_thermostat(self, temp: int) -> str:
        result = self._ha_call("POST", "/api/services/climate/set_temperature",
                                {"entity_id": "climate.thermostat", "temperature": temp})
        if result is None:
            return "Couldn't reach Home Assistant to set the thermostat."
        return f"Thermostat set to {temp} degrees."

    def _lock_device(self, device: str, lock: bool) -> str:
        service = "lock" if lock else "unlock"
        entity_id = self._guess_entity_id(device, domain="lock")
        result = self._ha_call("POST", f"/api/services/lock/{service}",
                                {"entity_id": entity_id})
        if result is None:
            return f"Couldn't reach Home Assistant to {service} {device}."
        return f"{device.capitalize()} {'locked' if lock else 'unlocked'}."

    @staticmethod
    def _guess_entity_id(device: str, domain: str = "light") -> str:
        slug = re.sub(r"[^a-z0-9]+", "_", device.lower()).strip("_")
        return f"{domain}.{slug}"

    # ------------------------------------------------------------------
    # Config interface
    # ------------------------------------------------------------------

    @property
    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="base_url",
                label="Home Assistant URL",
                type="str",
                default="",
                description="e.g. http://homeassistant.local:8123",
            ),
            ConfigField(
                name="token",
                label="Long-lived access token",
                type="str",
                default="",
                description="Create in HA → Profile → Long-lived access tokens.",
                secret=True,
            ),
            ConfigField(
                name="default_room",
                label="Default room",
                type="str",
                default="",
                description="Prefix for entity IDs when no room is specified.",
            ),
        ]

    def get_config(self) -> dict:
        return {
            "base_url":     self._base_url,
            "token":        "****" if self._token else "",
            "default_room": self._default_room,
        }

    def set_config(self, key: str, value: Any) -> None:
        if key == "base_url":
            self._base_url = str(value).strip()
        elif key == "token":
            self._token = str(value).strip()
        elif key == "default_room":
            self._default_room = str(value).strip()
        else:
            raise ValueError(f"SmartHomeSkill has no field {key!r}")
