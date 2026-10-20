"""Skill: pan the head servo left, right, or back to centre."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

# Legacy fallback for buses without motion settings.
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
            re.compile(r"(look|tilt) (up|down)"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        settings = bus.last("motion.settings") or {}
        if not isinstance(settings, dict):
            settings = {}
        mn = settings.get("soft_min_deg", _LEFT)
        mx = settings.get("soft_max_deg", _RIGHT)
        center = (mn + mx) / 2 if settings else _CENTER
        if match.group(0).endswith(("up", "down")):
            angle = settings.get("tilt_min_deg", 70) if "up" in lower else settings.get("tilt_max_deg", 110)
            bus.publish("motion.tilt_to", {"angle": angle})
            return "Looking up." if "up" in lower else "Looking down."
        if "left" in lower:
            bus.publish("motion.pan_to", {"angle": mn})
            return "Looking left."
        if "right" in lower:
            bus.publish("motion.pan_to", {"angle": mx})
            return "Looking right."
        bus.publish("motion.pan_to", {"angle": center})
        return "Looking ahead."
