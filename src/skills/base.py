"""Skills framework — abstract base class and registry.

A :class:`Skill` is a bundle of compiled regex patterns (tested against the
lower-cased utterance) plus a :meth:`handle` method that executes the intent
and optionally returns a spoken-response string.

:class:`SkillRegistry` holds an ordered list of skills and dispatches incoming
utterances to the *first* matching skill.

Bus contract
------------
* If ``handle()`` returns a non-empty string, the registry publishes it on
  ``av.say`` automatically.
* If ``handle()`` returns ``None``, the skill has already dispatched its own
  bus events (e.g. ``vision.describe``) and the registry stays silent.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from typing import Optional

log = logging.getLogger(__name__)


class Skill(ABC):
    """Abstract base for a single voice skill."""

    #: Short human-readable name used in log output.
    name: str = ""

    @property
    @abstractmethod
    def patterns(self) -> list[re.Pattern]:
        """Compiled regexes tested (via ``search``) against the lower-cased utterance."""

    def match(self, text: str) -> Optional[re.Match]:
        """Return the first :class:`re.Match` object, or ``None`` if nothing matches."""
        lower = text.lower().strip()
        for p in self.patterns:
            m = p.search(lower)
            if m:
                return m
        return None

    @abstractmethod
    def handle(self, text: str, match: re.Match, bus) -> Optional[str]:
        """Execute the skill.

        Parameters
        ----------
        text  : str
            Original utterance (not lower-cased).
        match : re.Match
            The regex match that triggered this skill.
        bus   : MessageBus
            In-process message bus; use ``bus.publish(topic, payload)`` to
            dispatch to other services.

        Returns
        -------
        str | None
            Spoken response text (the registry publishes it via ``av.say``),
            or ``None`` if the skill dispatched its own bus events.
        """


class SkillRegistry:
    """Ordered registry of skills; dispatches utterances to the first match."""

    def __init__(self) -> None:
        self._skills: list[Skill] = []

    def register(self, skill: Skill) -> None:
        """Append *skill* to the end of the dispatch chain."""
        self._skills.append(skill)

    @property
    def skill_names(self) -> list[str]:
        return [s.name for s in self._skills]

    def dispatch(self, text: str, bus) -> bool:
        """Try each skill in registration order; execute the first match.

        Returns
        -------
        bool
            ``True`` if a skill matched, ``False`` if no skill matched.
        """
        for skill in self._skills:
            m = skill.match(text)
            if m is not None:
                try:
                    response = skill.handle(text, m, bus)
                    if response:
                        bus.publish("av.say", {"text": response})
                except Exception:
                    log.exception("Skill %r raised while handling %r", skill.name, text)
                return True
        return False
