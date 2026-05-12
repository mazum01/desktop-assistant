"""Skill: control Pandora playback (play, stop, pause, skip, rate)."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill

# Each entry: (pattern, bus_topic, payload, spoken_response)
_ACTIONS = [
    (re.compile(r"(play|start|resume) (the )?music"),               "music.play",        {}, "Starting music."),
    (re.compile(r"(stop|turn off) (the )?music"),                   "music.stop",        {}, "Stopping music."),
    (re.compile(r"pause (the )?music"),                              "music.pause",       {}, "Music paused."),
    (re.compile(r"(skip|next) (song|track)?\s*$|next (song|track)"), "music.next",        {}, "Skipping to the next song."),
    (re.compile(r"\b(dislike|thumbs[\s-]?down|hate)\b"),            "music.thumbs_down", {}, "Thumbs down."),
    (re.compile(r"\b(like|thumbs[\s-]?up|rate up|love)\b"),         "music.thumbs_up",   {}, "Thumbs up!"),
]


class MusicControlSkill(Skill):
    name = "music_control"

    @property
    def patterns(self) -> list[re.Pattern]:
        return [a[0] for a in _ACTIONS]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()
        for pattern, topic, payload, response in _ACTIONS:
            if pattern.search(lower):
                bus.publish(topic, payload)
                return response
        return None
