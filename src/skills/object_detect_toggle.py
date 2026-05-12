"""Skill: enable or disable the object detection pipeline."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class ObjectDetectToggleSkill(Skill):
    name = "object_detect_toggle"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"(enable|start|turn on) (object detection|objects)"),
            re.compile(r"(disable|stop|turn off) (object detection|objects)"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        if re.search(r"\b(enable|start|turn on)\b", lower):
            bus.publish("object.set_enabled", {"enabled": True})
            return "Object detection enabled."
        bus.publish("object.set_enabled", {"enabled": False})
        return "Object detection disabled."
