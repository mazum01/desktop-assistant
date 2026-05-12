"""Skill: assign a name to the most recently seen face."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class MeetFaceSkill(Skill):
    """Publishes ``face.meet``; FaceService speaks the confirmation."""

    name = "meet_face"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"my name is (?P<name>[a-z][a-z '-]{1,39})", re.IGNORECASE),
            re.compile(r"(?:call|name) me (?P<name>[a-z][a-z '-]{1,39})", re.IGNORECASE),
            re.compile(r"i'?m (?P<name>[a-z][a-z '-]{1,29})\s*$", re.IGNORECASE),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        raw = match.group("name").strip()
        name = raw.title()
        if not name:
            return None
        bus.publish("face.meet", {"name": name})
        return None  # FaceService speaks "Nice to meet you, <name>!"
