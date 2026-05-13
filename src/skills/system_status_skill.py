"""Skill: report system temperature and CPU usage."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class SystemStatusSkill(Skill):
    """Reads live telemetry injected by SkillsService via a shared dict."""

    name = "system_status"

    def __init__(self, live_data: dict) -> None:
        self._data = live_data  # updated externally by SkillsService

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bhow hot (are you|is it)\b"),
            re.compile(r"\b(what('s| is) (your |the )?(cpu|temperature|temp|system status))\b"),
            re.compile(r"\b(are you running hot|system status|status report)\b"),
            re.compile(r"\bhow are you (doing|feeling|running)\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        temp = self._data.get("temperature")
        fan = self._data.get("fan_duty")
        cpu = self._data.get("cpu_percent")

        parts: list[str] = []
        if temp is not None:
            parts.append(f"My CPU temperature is {temp:.1f} degrees Celsius")
        if fan is not None:
            parts.append(f"fan is at {fan:.0f} percent")
        if cpu is not None:
            parts.append(f"CPU usage is {cpu:.0f} percent")

        if not parts:
            return "I don't have telemetry data available right now."
        return ", ".join(parts) + "."
