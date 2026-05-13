"""Skill: enable, disable, or query quiet hours."""

from __future__ import annotations

import re
from typing import Optional

from .base import Skill


class QuietHoursSkill(Skill):
    """Toggles quiet hours; optionally reports current state.

    The ``quiet_hours`` argument is a :class:`~src.core.quiet_hours.QuietHours`
    instance shared with the rest of the system (same object used by
    FaceService, AVService, etc.).  Pass ``None`` to disable this skill.
    """

    name = "quiet_hours"

    def __init__(self, quiet_hours=None) -> None:
        super().__init__()
        self._qh = quiet_hours

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\benable quiet hours\b"),
            re.compile(r"\bdisable quiet hours\b"),
            re.compile(r"\bturn (on|off) quiet (hours|mode)\b"),
            re.compile(r"\b(are (we|you) in quiet (hours|mode)|quiet hours (on|off|status))\b"),
            re.compile(r"\bwhen do you (sleep|go quiet)\b"),
        ]

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        if self._qh is None:
            return "Quiet hours are not configured."

        lower = text.lower()
        if re.search(r"\benable\b|\bturn on\b", lower):
            self._qh.update(True, self._qh.start, self._qh.end)
            bus.publish("settings.quiet_hours_updated", {
                "enabled": True,
                "start":   self._qh.start,
                "end":     self._qh.end,
            })
            return f"Quiet hours enabled. I'll be silent from {self._qh.start} to {self._qh.end}."

        if re.search(r"\bdisable\b|\bturn off\b", lower):
            self._qh.update(False, self._qh.start, self._qh.end)
            bus.publish("settings.quiet_hours_updated", {
                "enabled": False,
                "start":   self._qh.start,
                "end":     self._qh.end,
            })
            return "Quiet hours disabled. I'll speak anytime."

        # Status query
        if self._qh.enabled:
            state = "active right now" if self._qh.is_quiet() else "scheduled"
            return (
                f"Quiet hours are {state}, from {self._qh.start} to {self._qh.end}."
            )
        return "Quiet hours are currently disabled."
