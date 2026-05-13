"""Skill: set and fire spoken reminders.

Voice examples
--------------
  "remind me to take my medication in 30 minutes"
  "remind me to call mom in 2 hours"
  "remind me to check the oven at 6:30"
  "what are my reminders"
  "clear all reminders"
"""

from __future__ import annotations

import re
import threading
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from .base import ConfigField, Skill

log = logging.getLogger(__name__)

# Check interval for the background thread
_POLL_S = 10.0


@dataclass
class _Reminder:
    text: str
    fire_at: float  # monotonic timestamp


class ReminderSkill(Skill):
    """Set reminders that fire via spoken TTS when due."""

    name = "reminder"

    def __init__(self) -> None:
        super().__init__()
        self._snooze_min: int = 5
        self._reminders: list[_Reminder] = []
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._running = False
        self._bus = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, bus) -> None:
        self._bus = bus
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True, name="reminder-skill")
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            now = time.monotonic()
            fired: list[_Reminder] = []
            with self._lock:
                remaining: list[_Reminder] = []
                for r in self._reminders:
                    if now >= r.fire_at:
                        fired.append(r)
                    else:
                        remaining.append(r)
                self._reminders = remaining
            for r in fired:
                log.info("ReminderSkill: firing %r", r.text)
                if self._bus:
                    self._bus.publish("av.say", {"text": f"Reminder: {r.text}"})
            time.sleep(_POLL_S)

    # ------------------------------------------------------------------
    # Patterns
    # ------------------------------------------------------------------

    @property
    def patterns(self) -> list[re.Pattern]:
        return [
            re.compile(r"\bremind me (to .+?) in (\d+) (minute|minutes|min|hour|hours|hr)\b"),
            re.compile(r"\bremind me (to .+?) at (\d{1,2}):(\d{2})\b"),
            re.compile(r"\bset a? ?reminder (to .+?) in (\d+) (minute|minutes|min|hour|hours|hr)\b"),
            re.compile(r"\b(what are my reminders|list reminders|show reminders)\b"),
            re.compile(r"\b(clear all reminders|delete all reminders|cancel all reminders)\b"),
            re.compile(r"\bremind me\b"),  # catch-all for unrecognised formats
        ]

    # ------------------------------------------------------------------
    # Handle
    # ------------------------------------------------------------------

    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        lower = text.lower()

        # List reminders
        if re.search(r"\b(what are|list|show) (my )?reminders\b", lower):
            with self._lock:
                if not self._reminders:
                    return "You have no pending reminders."
                lines = []
                for r in self._reminders:
                    secs = max(0, r.fire_at - time.monotonic())
                    mins = int(secs // 60)
                    lines.append(f"{r.text} in {mins} minute{'s' if mins != 1 else ''}")
                return "Your reminders: " + "; ".join(lines) + "."

        # Clear all
        if re.search(r"\b(clear|delete|cancel) all reminders\b", lower):
            with self._lock:
                count = len(self._reminders)
                self._reminders.clear()
            return f"Cleared {count} reminder{'s' if count != 1 else ''}."

        # "in N minutes/hours"
        m_rel = re.search(
            r"remind me (?:to )?(.+?) in (\d+)\s*(minute|min|hour|hr)s?\b",
            lower,
        )
        if m_rel:
            task    = m_rel.group(1).strip()
            amount  = int(m_rel.group(2))
            unit    = m_rel.group(3)
            delay_s = amount * (3600 if unit in ("hour", "hr") else 60)
            self._add(task, delay_s, bus)
            unit_word = "hour" if unit in ("hour", "hr") else "minute"
            return f"Okay, I'll remind you to {task} in {amount} {unit_word}{'s' if amount != 1 else ''}."

        # "at HH:MM"
        m_abs = re.search(r"remind me (?:to )?(.+?) at (\d{1,2}):(\d{2})\b", lower)
        if m_abs:
            task = m_abs.group(1).strip()
            hour = int(m_abs.group(2))
            minute = int(m_abs.group(3))
            now_dt = datetime.now()
            target = now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now_dt:
                # Fire tomorrow
                from datetime import timedelta
                target += timedelta(days=1)
            delay_s = (target - now_dt).total_seconds()
            self._add(task, delay_s, bus)
            return f"Okay, I'll remind you to {task} at {hour:02d}:{minute:02d}."

        return "I didn't quite catch that reminder. Try 'remind me to call mom in 20 minutes'."

    def _add(self, task: str, delay_s: float, bus) -> None:
        if self._bus is None:
            self._bus = bus
        r = _Reminder(text=task, fire_at=time.monotonic() + delay_s)
        with self._lock:
            self._reminders.append(r)
        log.info("ReminderSkill: scheduled %r in %.0fs", task, delay_s)

    # ------------------------------------------------------------------
    # Config interface
    # ------------------------------------------------------------------

    @property
    def config_schema(self) -> list[ConfigField]:
        return [
            ConfigField(
                name="snooze_min",
                label="Default snooze (minutes)",
                type="int",
                default=5,
                min=1,
                max=60,
                description="Minutes to snooze when a reminder is dismissed.",
            ),
            ConfigField(
                name="pending",
                label="Pending reminders",
                type="display",
                default="",
                description="Read-only list of scheduled reminders.",
            ),
        ]

    def get_config(self) -> dict:
        with self._lock:
            pending_lines = []
            for r in self._reminders:
                secs = max(0, r.fire_at - time.monotonic())
                mins = int(secs // 60)
                pending_lines.append(f"{r.text} (in {mins}m)")
        return {
            "snooze_min": self._snooze_min,
            "pending":    ", ".join(pending_lines) if pending_lines else "none",
        }

    def set_config(self, key: str, value: Any) -> None:
        if key == "snooze_min":
            v = int(value)
            if not 1 <= v <= 60:
                raise ValueError("snooze_min must be 1–60")
            self._snooze_min = v
        else:
            raise ValueError(f"ReminderSkill has no writable field {key!r}")
