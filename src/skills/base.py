"""Skills framework — abstract base class, config schema, and registry.

A :class:`Skill` is a bundle of compiled regex patterns (tested against the
lower-cased utterance) plus a :meth:`handle` method that executes the intent
and optionally returns a spoken-response string.

Skills may optionally expose configuration via :attr:`config_schema`,
:meth:`get_config`, and :meth:`set_config`.  This allows the web GUI and CLI
to display and edit per-skill settings without touching YAML files.

:class:`SkillRegistry` holds an ordered list of skills and dispatches incoming
utterances to the *first* matching, *enabled* skill.

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
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config field descriptor
# ---------------------------------------------------------------------------

@dataclass
class ConfigField:
    """Describes one configurable parameter exposed by a :class:`Skill`.

    Parameters
    ----------
    name        : str   Internal key used in ``get_config``/``set_config``.
    label       : str   Human-readable label shown in the UI.
    type        : str   One of ``"bool"``, ``"int"``, ``"float"``, ``"str"``,
                        ``"select"``, or ``"display"`` (read-only).
    default     : Any   Default value before any explicit configuration.
    description : str   Short help text shown below the field.
    options     : list  For ``type="select"``, the list of valid string values.
    min         : float For numeric types, optional lower bound.
    max         : float For numeric types, optional upper bound.
    secret      : bool  If True, the value is masked in the UI (e.g. API token).
    """

    name: str
    label: str
    type: Literal["bool", "int", "float", "str", "select", "display"] = "str"
    default: Any = None
    description: str = ""
    options: list[str] = field(default_factory=list)
    min: Optional[float] = None
    max: Optional[float] = None
    secret: bool = False

    def as_dict(self) -> dict:
        return {
            "name":        self.name,
            "label":       self.label,
            "type":        self.type,
            "default":     self.default,
            "description": self.description,
            "options":     self.options,
            "min":         self.min,
            "max":         self.max,
            "secret":      self.secret,
        }


# ---------------------------------------------------------------------------
# Skill ABC
# ---------------------------------------------------------------------------

class Skill(ABC):
    """Abstract base for a single voice skill."""

    #: Short human-readable name used in log output and as the config key.
    name: str = ""

    #: When ``False`` the registry skips this skill entirely.
    enabled: bool = True

    def __init__(self) -> None:
        # Ensure instance attribute is set (subclasses may skip super().__init__)
        if not hasattr(self, "enabled"):
            self.enabled = True

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

    # ------------------------------------------------------------------
    # Optional config interface — override in skills that have settings
    # ------------------------------------------------------------------

    @property
    def config_schema(self) -> list[ConfigField]:
        """Declare configurable fields.  Override to expose settings in the UI."""
        return []

    def get_config(self) -> dict:
        """Return current configuration as ``{name: value}``."""
        return {}

    def set_config(self, key: str, value: Any) -> None:
        """Apply a single config change.  Raise :class:`ValueError` for unknowns."""
        raise ValueError(f"Skill {self.name!r} has no configurable field {key!r}")

    # ------------------------------------------------------------------
    # Optional lifecycle hooks — override for skills with background threads
    # ------------------------------------------------------------------

    def start(self, bus) -> None:
        """Called by :class:`SkillsService` when the service starts.  Override if needed."""

    def stop(self) -> None:
        """Called by :class:`SkillsService` when the service stops.  Override if needed."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class SkillRegistry:
    """Ordered registry of skills; dispatches utterances to the first enabled match."""

    def __init__(self) -> None:
        self._skills: list[Skill] = []

    def register(self, skill: Skill) -> None:
        """Append *skill* to the end of the dispatch chain."""
        self._skills.append(skill)

    @property
    def skill_names(self) -> list[str]:
        return [s.name for s in self._skills]

    @property
    def skills(self) -> list[Skill]:
        """Return the ordered list of all registered skills (read-only view)."""
        return list(self._skills)

    def find(self, name: str) -> Optional[Skill]:
        """Return the skill with the given name, or ``None``."""
        for s in self._skills:
            if s.name == name:
                return s
        return None

    def dispatch(self, text: str, bus) -> bool:
        """Try each *enabled* skill in registration order; execute the first match.

        Returns
        -------
        bool
            ``True`` if a skill matched and ran, ``False`` if no skill matched.
        """
        for skill in self._skills:
            if not skill.enabled:
                continue
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
