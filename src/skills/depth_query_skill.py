"""Skill: query depth of the current scene."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class DepthQuerySkill(Skill):
    """Respond to depth/range queries by speaking a depth summary."""

    name = "depth_query"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"how (far|close|near) (is|are)"),
            re.compile(r"(depth|range) (scan|report|check)"),
            re.compile(r"what('s| is) (nearest|closest|farthest|the distance)"),
            re.compile(r"how (far|close) away"),
            re.compile(r"distance (to|from) (the )?"),
            re.compile(r"scan (the )?room"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        bus.publish("depth.query", {})
        return None  # DepthQueryHandler in skills_service speaks the response
