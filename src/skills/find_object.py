"""Skill: find an object by a free-form prompt."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class FindObjectSkill(Skill):
    name = "find_object"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"(find|look for|search for)\s+(?:the\s+|a\s+|an\s+)?(?P<query>.+)"),
            re.compile(r"(do you see|can you see|is there|where is)\s+(?:the\s+|a\s+|an\s+|any\s+)?(?P<query>.+)"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        query = (match.groupdict().get("query") or match.group(1) or text).strip()
        bus.publish("vision.object_query", {"query": query, "speak": True})
        return None
