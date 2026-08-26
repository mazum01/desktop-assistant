"""Skill: describe what VERA can do."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

_CAPABILITIES = (
    "I can tell you the time, tell a joke, describe what I see, or find objects by name. "
    "I can greet you, name faces, control music playback, pan my head, "
    "toggle face tracking, object detection, or quiet hours. "
    "I can also report my system temperature and CPU usage. "
    "Just speak naturally and I'll do my best to help."
)


class HelpSkill(Skill):
    name = "help"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bwhat can you do\b"),
            re.compile(r"\blist (your )?skills\b"),
            re.compile(r"\bwhat (are your|do you have for) (skills|capabilities)\b"),
            re.compile(r"\bhelp\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        return _CAPABILITIES
