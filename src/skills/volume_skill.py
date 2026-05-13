"""Skill: adjust music volume by voice."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

# Step size for relative volume changes
_STEP = 10


class VolumeSkill(Skill):
    name = "volume"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bset (the )?volume to (\d+)\b"),
            re.compile(r"\bvolume (up|down|\d+)\b"),
            re.compile(r"\b(turn it|turn the volume) (up|down)\b"),
            re.compile(r"\b(louder|quieter|softer)\b"),
            re.compile(r"\bmute (the )?(music|sound)?\b"),
            re.compile(r"\bmax(imum)? volume\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()

        # Mute
        if re.search(r"\bmute\b", lower):
            bus.publish("music.set_volume", {"level": 0})
            return "Music muted."

        # Max volume
        if re.search(r"\bmax(imum)?\b", lower):
            bus.publish("music.set_volume", {"level": 100})
            return "Volume set to maximum."

        # Absolute: "set volume to 60" or "volume 60"
        m = re.search(r"\b(\d+)\b", lower)
        if m and re.search(r"\bset (the )?volume\b|volume \d+", lower):
            level = max(0, min(100, int(m.group(1))))
            bus.publish("music.set_volume", {"level": level})
            return f"Volume set to {level}."

        # Relative up
        if re.search(r"\b(up|louder|turn it up|turn the volume up)\b", lower):
            bus.publish("music.set_volume", {"delta": _STEP})
            return "Volume up."

        # Relative down
        if re.search(r"\b(down|quieter|softer|turn it down|turn the volume down)\b", lower):
            bus.publish("music.set_volume", {"delta": -_STEP})
            return "Volume down."

        return "Sorry, I didn't catch a volume level. Try saying 'set volume to 50' or 'louder'."
