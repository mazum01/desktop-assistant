"""Skill: conversational greetings — responds with a time-aware reply."""

from __future__ import annotations

import random
import re
from datetime import datetime
from typing import Optional

from .base import Skill

_RESPONSES: dict[str, list[str]] = {
    "morning": [
        "Good morning! How can I help?",
        "Morning! What can I do for you?",
        "Good morning! Ready when you are.",
    ],
    "afternoon": [
        "Good afternoon! What can I do for you?",
        "Afternoon! How can I help?",
        "Hey there! Good afternoon.",
    ],
    "evening": [
        "Good evening! What do you need?",
        "Evening! How can I help?",
        "Hey! Good evening.",
    ],
    "generic": [
        "Hey there! How can I help?",
        "Hi! What can I do for you?",
        "Hello! How can I help?",
        "Hey! What's up?",
    ],
}


class GreetingSkill(Skill):
    name = "greeting"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\b(hello|hi|hey|howdy|greetings)\b"),
            re.compile(r"^good (morning|afternoon|evening|night)\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        hour = datetime.now().hour
        if 5 <= hour < 12:
            bucket = "morning"
        elif 12 <= hour < 17:
            bucket = "afternoon"
        elif 17 <= hour < 21:
            bucket = "evening"
        else:
            bucket = "generic"
        return random.choice(_RESPONSES[bucket])
