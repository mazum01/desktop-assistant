"""Intent routing for voice-command transcripts."""

from __future__ import annotations

import re
from dataclasses import dataclass

from src.audio.version_announcer import is_version_query


@dataclass(frozen=True)
class IntentDecision:
    name: str
    route: str
    confidence: float
    normalized_text: str


class IntentRouter:
    """Classify utterances into high-level action categories."""

    _YES_RE = re.compile(r"^\s*(yes|yeah|yep|confirm|do it|go ahead)\s*$", re.I)
    _NO_RE = re.compile(r"^\s*(no|nope|cancel|stop|never mind)\s*$", re.I)
    _REBOOT_RE = re.compile(r"\b(reboot|restart)\b", re.I)
    _SHUTDOWN_RE = re.compile(r"\b(shut\s*down|power\s*off)\b", re.I)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join((text or "").strip().split())

    def classify(self, text: str) -> IntentDecision:
        normalized = self._normalize(text)
        if not normalized:
            return IntentDecision("empty", "none", 1.0, normalized)
        if self._YES_RE.match(normalized):
            return IntentDecision("confirm", "dialog", 0.95, normalized)
        if self._NO_RE.match(normalized):
            return IntentDecision("deny", "dialog", 0.95, normalized)
        if is_version_query(normalized):
            return IntentDecision("version_query", "av_utterance", 0.99, normalized)
        if self._REBOOT_RE.search(normalized):
            return IntentDecision("reboot_request", "dialog_confirm", 0.9, normalized)
        if self._SHUTDOWN_RE.search(normalized):
            return IntentDecision("shutdown_request", "dialog_confirm", 0.9, normalized)
        return IntentDecision("skill_dispatch", "av_utterance", 0.75, normalized)
