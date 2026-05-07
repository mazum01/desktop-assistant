"""
Face greeting service.

Listens for identified faces on ``perception.faces``, speaks greetings via
``av.say``, and handles the ``face.meet`` bus event for name assignment (sent
by the CLI ``desktop-assistant meet <name>`` command).

Greeting rules
--------------
* **New face** (is_new=True): fixed introduction phrase, then ask for name.
* **Known face** (is_new=False): re-greet only if ``needs_greeting()`` is True
  (person absent ≥ *greeting_cooldown_s* seconds). Varies phrase each time.
* **Name assignment** (``face.meet``): names the most-recently-seen ungreeted
  face, speaks "Nice to meet you, <name>!".

Topics subscribed
-----------------
perception.faces    ``{faces: [{face_id, name, is_new, …}, …], …}``
face.meet           ``{name: str}``  — from CLI

Topics published
----------------
av.say              greeting text
face.identified     ``{face_id, name, is_new, score}``  — telemetry / debug
"""

from __future__ import annotations

import logging
import random
from typing import Optional

from src.core.bus import MessageBus
from src.core.quiet_hours import QuietHours
from src.core.service import Service

log = logging.getLogger(__name__)

# Varied re-greet phrases.  {name} is substituted at runtime.
_REGREET_PHRASES = [
    "Welcome back, {name}!",
    "Hey {name}, good to see you again.",
    "Hello again, {name}.",
    "Nice to see you, {name}.",
    "{name}! Good to have you back.",
    "Oh hey, {name}!",
    "Hi {name}, welcome back.",
    "Good to see you again, {name}.",
]

_NEW_FACE_PHRASE = (
    "Hi! I'm the Desktop Assistant. Nice to meet you. "
    "Can I have your name?"
)

_DEFAULT_COOLDOWN_S = 300.0   # 5 minutes


class FaceService(Service):
    """Greet known and new faces; handle CLI name assignment."""

    name = "face"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        registry=None,
        greeting_cooldown_s: float = _DEFAULT_COOLDOWN_S,
        quiet_hours: Optional[QuietHours] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._registry = registry
        self._cooldown = greeting_cooldown_s
        self._quiet_hours = quiet_hours
        self._unsubs: list = []
        self._last_phrase: Optional[str] = None   # avoid immediate repeat
        self._greeted_new_ids: set[str] = set()  # session-level guard: greet each new face only once

    def on_start(self) -> None:
        if self._registry is None:
            from src.perception.face_registry import FaceRegistry
            self._registry = FaceRegistry()

        self._unsubs.append(
            self.bus.subscribe("perception.faces", self._on_faces)
        )
        self._unsubs.append(
            self.bus.subscribe("face.meet", self._on_meet)
        )
        log.info("FaceService started (cooldown=%.0fs)", self._cooldown)

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._registry is not None:
            try:
                self._registry.close()
            except Exception:
                pass
        log.info("FaceService stopped")

    # ── Bus handlers ─────────────────────────────────────────────────────

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        for face in faces:
            face_id = face.get("face_id")
            name = face.get("name")
            is_new = face.get("is_new", False)
            score = face.get("match_score", 0.0)

            if not face_id or not name:
                continue

            # Publish identity event for telemetry/debug
            self.bus.publish("face.identified", {
                "face_id": face_id,
                "name": name,
                "is_new": is_new,
                "score": score,
            })

            if is_new:
                self._greet_new(face_id, name)
            elif self._registry.needs_greeting(face_id, self._cooldown):
                self._greet_returning(face_id, name)

    def _on_meet(self, _topic, payload) -> None:
        """CLI sent 'desktop-assistant meet <name>' — name the last seen face."""
        if not isinstance(payload, dict):
            return
        given_name = (payload.get("name") or "").strip()
        if not given_name:
            log.warning("face.meet received empty name")
            return

        if self._registry is None:
            return

        face_id = self._registry.get_current_face_id()
        if face_id is None:
            log.warning("face.meet: no faces in registry yet")
            self.bus.publish("av.say", {"text": "I haven't seen anyone yet to name."})
            return

        old_name = (self._registry.get_face(face_id) or {}).get("name", "")
        self._registry.set_name(face_id, given_name)
        self._registry.mark_greeted(face_id)
        text = f"Nice to meet you, {given_name}! I'll remember you."
        log.info("Named face %s: %r → %r", face_id[:8], old_name, given_name)
        self.bus.publish("av.say", {"text": text})

    # ── Greeting helpers ─────────────────────────────────────────────────

    def _greet_new(self, face_id: str, name: str) -> None:
        if face_id in self._greeted_new_ids:
            return  # already introduced this face this session
        self._greeted_new_ids.add(face_id)
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: new-face greeting suppressed — quiet hours active")
            return
        self._registry.mark_greeted(face_id)
        log.info("Greeting new face %s (%s)", face_id[:8], name)
        self.bus.publish("av.say", {"text": _NEW_FACE_PHRASE})

    def _greet_returning(self, face_id: str, name: str) -> None:
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: returning-face greeting suppressed — quiet hours active")
            return
        self._registry.mark_greeted(face_id)
        phrase = self._pick_phrase(name)
        log.info("Re-greeting %s (%s): %r", face_id[:8], name, phrase)
        self.bus.publish("av.say", {"text": phrase})

    def _pick_phrase(self, name: str) -> str:
        """Pick a varied greeting phrase, avoiding the immediately-previous one."""
        candidates = [p for p in _REGREET_PHRASES if p != self._last_phrase]
        if not candidates:
            candidates = _REGREET_PHRASES
        template = random.choice(candidates)
        self._last_phrase = template  # store template, not formatted string
        return template.format(name=name)
