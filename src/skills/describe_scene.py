"""Skill: describe the current scene (objects + faces)."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class DescribeSceneSkill(Skill):
    """Publish ``vision.describe``; ObjectService speaks the result."""

    name = "describe_scene"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"what (do|can) you see"),
            re.compile(r"describe (the )?scene"),
            re.compile(r"what('s| is) (in front|around|there)"),
            re.compile(r"look around"),
            re.compile(r"what('s| is) going on"),
            re.compile(r"tell me what you see"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        bus.publish("vision.describe", {})
        return None  # ObjectService speaks the description
