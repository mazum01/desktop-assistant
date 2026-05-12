"""Skill: announce the current local time."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from .base import Skill


class TellTimeSkill(Skill):
    name = "tell_time"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"what('s| is) (the )?time"),
            re.compile(r"what time is it"),
            re.compile(r"tell me the time"),
            re.compile(r"\bcurrent time\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        now = datetime.now()
        h12 = now.hour % 12 or 12
        ampm = "AM" if now.hour < 12 else "PM"
        if now.minute == 0:
            return f"It's {h12} o'clock {ampm}."
        return f"It's {h12}:{now.minute:02d} {ampm}."
