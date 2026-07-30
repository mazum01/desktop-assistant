"""Skill: enable or disable use of the Anthropic Claude API."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class AnthropicToggleSkill(Skill):
    name = "anthropic_toggle"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"(enable|start|turn on) (the )?(anthropic|claude)( api)?"),
            re.compile(r"(disable|stop|turn off) (the )?(anthropic|claude)( api)?"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        if re.search(r"\b(enable|start|turn on)\b", lower):
            bus.publish("anthropic.set_enabled", {"enabled": True})
            return "Anthropic API enabled."
        bus.publish("anthropic.set_enabled", {"enabled": False})
        return "Anthropic API disabled."
