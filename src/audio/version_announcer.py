"""
Spoken-version announcer (FR-VR1 .. FR-VR4).

The assistant must:
- announce its version at startup (FR-VR1)
- respond to verbal queries like "what version are you" (FR-VR2)
- read /VERSION as the single source of truth (FR-VR3)
- be callable by any service (FR-VR4)

This module wires `core.version.spoken_version()` to the TTS layer.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from src.audio.tts import TextToSpeech
from src.core.version import get_version, spoken_version

log = logging.getLogger(__name__)

# Phrases that should trigger a verbal version response.
_VERSION_QUERY_PATTERNS = [
    r"\bwhat(?:'s| is)? your version\b",
    r"\bwhat version are you\b",
    r"\bversion number\b",
    r"\btell me (?:the |your )?version\b",
    r"\bwhich version\b",
]
_VERSION_QUERY_RE = re.compile("|".join(_VERSION_QUERY_PATTERNS), re.IGNORECASE)


def is_version_query(utterance: str) -> bool:
    """True if *utterance* is asking for the version number."""
    if not utterance:
        return False
    return bool(_VERSION_QUERY_RE.search(utterance))


class VersionAnnouncer:
    """
    Speaks the project version through TTS. Used by the boot sequence
    (`announce_startup`) and the dialog handler (`announce_on_request`).
    """

    def __init__(
        self,
        tts: Optional[TextToSpeech] = None,
        audio_output=None,
    ) -> None:
        self._tts = tts or TextToSpeech()
        self._output = audio_output

    def speak_version(self, prefix: str = "") -> None:
        """Speak the current version. Optional *prefix* prepends a phrase."""
        phrase = (prefix + " " if prefix else "") + spoken_version()
        log.info("Speaking version: %s (%s)", get_version(), phrase)
        self._tts.say(phrase, output=self._output)

    def announce_startup(self) -> None:
        """Boot-time announcement (FR-VR1)."""
        self.speak_version(prefix="Desktop assistant starting,")

    def announce_on_request(self) -> None:
        """Verbal-query response (FR-VR2)."""
        self.speak_version(prefix="I am running")

    def maybe_handle(self, utterance: str) -> bool:
        """If *utterance* is a version query, respond. Returns True if handled."""
        if is_version_query(utterance):
            self.announce_on_request()
            return True
        return False
