"""Skill: report current weather conditions via wttr.in (no API key required)."""

from __future__ import annotations

import re
import urllib.request
import urllib.error
import json
import logging
from typing import Any, Optional

from .base import ConfigField, Skill

log = logging.getLogger(__name__)

_WTTR_URL = "https://wttr.in/{location}?format=j1"
_TIMEOUT_S = 4


def _fetch_weather(location: str) -> dict | None:
    url = _WTTR_URL.format(location=urllib.request.quote(location) if location != "auto" else "")
    try:
        with urllib.request.urlopen(url, timeout=_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("WeatherSkill: fetch failed: %s", exc)
        return None


def _format_temp(celsius: float, units: str) -> str:
    if units == "imperial":
        f = celsius * 9 / 5 + 32
        return f"{f:.0f}°F"
    return f"{celsius:.0f}°C"


class WeatherSkill(Skill):
    """Fetch and speak current weather from wttr.in."""

    name = "weather"

    def __init__(self) -> None:
        super().__init__()
        self._location: str = "auto"
        self._units: str = "imperial"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\b(what('s| is) (the )?(weather|forecast))\b"),
            re.compile(r"\bweather (today|outside|right now|forecast)\b"),
            re.compile(r"\b(will it rain|is it (going to|gonna) rain)\b"),
            re.compile(r"\b(how (hot|cold|warm) is it (outside|today)?)\b"),
            re.compile(r"\b(temperature outside|outside temperature)\b"),
            re.compile(r"\bdo i need (a|an) (umbrella|jacket|coat)\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        data = _fetch_weather(self._location)
        if data is None:
            return "Sorry, I couldn't reach the weather service right now."

        try:
            current = data["current_condition"][0]
            temp_c  = float(current["temp_C"])
            feels_c = float(current["FeelsLikeC"])
            desc    = current["weatherDesc"][0]["value"]
            humidity = current.get("humidity", "?")

            area = ""
            try:
                area = data["nearest_area"][0]["areaName"][0]["value"]
            except Exception:
                pass

            temp_str  = _format_temp(temp_c, self._units)
            feels_str = _format_temp(feels_c, self._units)
            location_str = f" in {area}" if area and self._location == "auto" else ""

            return (
                f"Currently{location_str}: {desc}, {temp_str}. "
                f"Feels like {feels_str}. Humidity {humidity} percent."
            )
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("WeatherSkill: parse error: %s", exc)
            return "I got a weather response but couldn't parse it."

    # ------------------------------------------------------------------
    # Config interface
    # ------------------------------------------------------------------

    @property
    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="location",
                label="Location",
                type="str",
                default="auto",
                description='City name, zip code, or "auto" to detect from IP.',
            ),
            ConfigField(
                name="units",
                label="Temperature units",
                type="select",
                default="imperial",
                options=["imperial", "metric"],
                description="Imperial = °F, Metric = °C.",
            ),
        ]

    def get_config(self) -> dict:
        return {"location": self._location, "units": self._units}

    def set_config(self, key: str, value: Any) -> None:
        if key == "location":
            self._location = str(value).strip() or "auto"
        elif key == "units":
            if value not in ("imperial", "metric"):
                raise ValueError("units must be 'imperial' or 'metric'")
            self._units = value
        else:
            raise ValueError(f"WeatherSkill has no field {key!r}")
