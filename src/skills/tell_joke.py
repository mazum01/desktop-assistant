"""Skill: tell a dad joke via ClockService."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class TellJokeSkill(Skill):
    name = "tell_joke"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"(tell|say|give|know any) (me )?(a |any )?(dad |good |funny )?joke"),
            re.compile(r"(make me|something) (funny|laugh)"),
            re.compile(r"\bdad joke\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        bus.publish("av.tell_joke", {})
        return None  # ClockService fetches and speaks the joke
