"""Skill: pan the head servo left, right, or back to centre."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

# These mirror the default soft-limit config in assistant.yaml.
_CENTER = 180.0
_LEFT   = 145.0
_RIGHT  = 215.0


class MotionControlSkill(Skill):
    name = "motion_control"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"look (to the |to )?(left|right|center|forward|straight|ahead)"),
            re.compile(r"(face|turn|pan) (left|right|center|forward|straight|ahead)"),
            re.compile(r"look (straight ahead|forward|ahead|center)"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        if "left" in lower:
            bus.publish("motion.pan_to", {"angle": _LEFT})
            return "Looking left."
        if "right" in lower:
            bus.publish("motion.pan_to", {"angle": _RIGHT})
            return "Looking right."
        bus.publish("motion.pan_to", {"angle": _CENTER})
        return "Looking ahead."
