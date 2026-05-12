"""Skill: enable or disable face-tracking (servo follows faces)."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

_ENABLE_RE  = re.compile(r"\b(enable|start|turn on)\b|follow me|\btrack(ing)? my face\b")
_DISABLE_RE = re.compile(
    r"\b(disable|turn off|stop)\b.*(follow|face[\s-]?tracking)"
    r"|don'?t.*(follow|track)"
    r"|\bstop following\b"
)


class FaceTrackingToggleSkill(Skill):
    name = "face_tracking_toggle"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"(enable|start|turn on) face tracking"),
            re.compile(r"(disable|stop|turn off) face tracking"),
            re.compile(r"(stop|don'?t) follow(ing)? me"),
            re.compile(r"\bfollow me\b"),
            re.compile(r"track(ing)? my face"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        if _DISABLE_RE.search(lower):
            bus.publish("tracking.set_face_tracking", {"enabled": False})
            return "Face tracking disabled."
        bus.publish("tracking.set_face_tracking", {"enabled": True})
        return "Face tracking enabled."
