"""Minimal dialog/session state for multi-turn voice commands."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from src.voice.intent_router import IntentDecision


@dataclass
class DialogManagerConfig:
    session_timeout_s: float = 20.0


@dataclass
class DialogState:
    session_id: int = 0
    turns: int = 0
    last_intent: str = "none"
    updated_mono: float = field(default_factory=time.monotonic)
    pending_confirmation: str | None = None
    pending_payload: dict[str, Any] | None = None


class DialogManager:
    """Tracks short-lived voice context and confirmation flow."""

    def __init__(self, config: DialogManagerConfig | None = None) -> None:
        self._cfg = config or DialogManagerConfig()
        self._state = DialogState()

    def _expire_if_needed(self, now_mono: float | None = None) -> None:
        now = time.monotonic() if now_mono is None else float(now_mono)
        if (now - self._state.updated_mono) <= float(self._cfg.session_timeout_s):
            return
        self._state = DialogState(session_id=self._state.session_id + 1)

    def observe(self, decision: IntentDecision, now_mono: float | None = None) -> None:
        self._expire_if_needed(now_mono)
        self._state.turns += 1
        self._state.last_intent = decision.name
        self._state.updated_mono = time.monotonic() if now_mono is None else float(now_mono)

    def set_pending_confirmation(
        self, action: str, payload: dict[str, Any] | None = None, now_mono: float | None = None
    ) -> None:
        self._expire_if_needed(now_mono)
        self._state.pending_confirmation = action
        self._state.pending_payload = dict(payload or {})
        self._state.updated_mono = time.monotonic() if now_mono is None else float(now_mono)

    def resolve_confirmation(
        self, decision: IntentDecision, now_mono: float | None = None
    ) -> tuple[bool, dict[str, Any] | None]:
        self._expire_if_needed(now_mono)
        pending = self._state.pending_confirmation
        if not pending:
            return False, None

        if decision.name == "confirm":
            payload = {"action": pending, **(self._state.pending_payload or {})}
            self._state.pending_confirmation = None
            self._state.pending_payload = None
            self._state.updated_mono = time.monotonic() if now_mono is None else float(now_mono)
            return True, payload

        if decision.name == "deny":
            self._state.pending_confirmation = None
            self._state.pending_payload = None
            self._state.updated_mono = time.monotonic() if now_mono is None else float(now_mono)
            return True, None

        return False, None

    def snapshot(self) -> dict[str, Any]:
        return {
            "session_id": self._state.session_id,
            "turns": self._state.turns,
            "last_intent": self._state.last_intent,
            "pending_confirmation": self._state.pending_confirmation,
            "has_pending_payload": bool(self._state.pending_payload),
        }
