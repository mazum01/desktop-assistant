"""
Face greeting service.

Listens for identified faces on ``perception.faces``, speaks greetings via
``av.say``, and handles the ``face.meet`` bus event for name assignment (sent
by the CLI ``desktop-assistant meet <name>`` command).

Greeting rules
--------------
* **Known face** (is_new=False): re-greet only if ``needs_greeting()`` is True —
  face was absent ≥ *min_absence_s*, cooldown (±jitter) has elapsed since last greet.
* **New face** (is_new=True): introduce once per session (in-memory guard).
* **Name assignment** (``face.meet``): names the most-recently-seen face,
  speaks a "Nice to meet you, <name>!" confirmation.

Absence detection
-----------------
Each incoming ``perception.faces`` payload is diffed against the previous frame.
Any face that disappears is marked absent in the registry after a 3-frame debounce,
so brief detection gaps don't reset the greeting cooldown.

Topics subscribed
-----------------
perception.faces    ``{faces: [{face_id, name, is_new, …}, …], …}``
face.meet           ``{name: str}``  — from CLI / web UI
tracking.set_greeting_cooldown  ``{cooldown_min: float}``

Topics published
----------------
av.say              greeting text
face.identified     ``{face_id, name, is_new, score}``  — telemetry / debug
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from typing import Optional

from src.core.bus import MessageBus
from src.core.quiet_hours import QuietHours
from src.core.service import Service

log = logging.getLogger(__name__)

# ── Time-aware greeting phrases for known faces ───────────────────────────────
# Keyed by time bucket; {name} substituted at runtime.
# IMPORTANT: avoid "word, {name}" patterns — a comma immediately before a name
# creates an awkward prosodic pause in Piper TTS. Put the name at the start of
# a clause, mid-sentence after "you", or before the comma instead.

_GREET_MORNING = [
    "Good morning {name}! Hope you slept well.",
    "Hey {name}! Ready to take on the day?",
    "{name}! Good morning!",
    "Rise and shine {name}!",
]

_GREET_AFTERNOON = [
    "Good afternoon {name}! How's your day going?",
    "Hey {name}! Good to see you this afternoon.",
    "{name}! Good afternoon!",
    "Welcome back {name}. Having a good afternoon?",
]

_GREET_EVENING = [
    "Good evening {name}! Winding down for the night?",
    "Hey {name}! Good evening.",
    "{name}! Hope your day was great.",
    "Nice to see you this evening {name}!",
]

_GREET_NIGHT = [
    "Still up {name}? Night owl mode!",
    "Hey {name}! Burning the midnight oil?",
    "{name}! Catching me at a late hour.",
    "Night {name}! Don't stay up too late.",
]

_GREET_GENERIC = [
    "Welcome back {name}!",
    "Hey {name}! Good to see you again.",
    "There you are {name}!",
    "Hello again {name}!",
    "{name}! Good to have you back.",
]

# ── Personality-filled intro phrases for new faces ────────────────────────────

_NEW_FACE_PHRASES = [
    "Oh hey, I don't think we've met! I'm Desktop Assistant. What's your name?",
    "Whoa, a new face! Hi there, I'm the Desktop Assistant. And you are?",
    "Hello! Don't think I know you yet. I'm Desktop Assistant. Mind introducing yourself?",
    "Hey! I'm Desktop Assistant. I'd love to know your name. What should I call you?",
    "Well, hello there! I'm Desktop Assistant. I haven't had the pleasure. What's your name?",
]

# Default cooldown: 30 minutes base, ±25% jitter
_DEFAULT_COOLDOWN_MIN  = 30.0
_DEFAULT_JITTER_PCT    = 25.0
_DEFAULT_MIN_ABSENCE_S = 30.0
_DEFAULT_CONFIDENCE    = 0.5

# Absence debounce: face must be missing this many consecutive frames
_ABSENCE_DEBOUNCE_FRAMES = 3


def _time_bucket() -> str:
    """Return morning / afternoon / evening / night based on local time."""
    hour = datetime.now().hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _jittered_cooldown(cooldown_min: float, jitter_pct: float) -> float:
    """Return cooldown_min ± jitter_pct%, converted to seconds."""
    jitter = cooldown_min * jitter_pct / 100.0
    return (cooldown_min + random.uniform(-jitter, jitter)) * 60.0


class FaceService(Service):
    """Greet known and new faces; handle CLI name assignment."""

    name = "face"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        registry=None,
        greeting_cooldown_min: float = _DEFAULT_COOLDOWN_MIN,
        greeting_cooldown_jitter_pct: float = _DEFAULT_JITTER_PCT,
        min_absence_s: float = _DEFAULT_MIN_ABSENCE_S,
        confidence_threshold: float = _DEFAULT_CONFIDENCE,
        quiet_hours: Optional[QuietHours] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._registry = registry
        self._cooldown_min = greeting_cooldown_min
        self._jitter_pct = greeting_cooldown_jitter_pct
        self._min_absence_s = min_absence_s
        self._confidence_threshold = confidence_threshold
        self._quiet_hours = quiet_hours
        self._unsubs: list = []
        self._last_phrase: Optional[str] = None
        self._greeted_new_ids: set[str] = set()   # session guard: intro each new face once
        self._prev_face_ids: set[str] = set()     # face_ids in previous frame
        self._absent_counter: dict[str, int] = {} # face_id → consecutive absent-frame count

    def on_start(self) -> None:
        if self._registry is None:
            from src.perception.face_registry import FaceRegistry
            self._registry = FaceRegistry()

        # Treat a (re)start as an absence event for every known face so that
        # anyone already in frame when we come up gets a proper greeting.
        self._registry.mark_all_absent()

        self._unsubs.append(self.bus.subscribe("perception.faces", self._on_faces))
        self._unsubs.append(self.bus.subscribe("face.meet", self._on_meet))
        self._unsubs.append(
            self.bus.subscribe("tracking.set_greeting_cooldown", self._on_set_cooldown)
        )
        self._unsubs.append(self.bus.subscribe("face.deleted", self._on_face_deleted))
        self._unsubs.append(self.bus.subscribe("face.guests_cleared", self._on_faces_cleared))
        self._unsubs.append(self.bus.subscribe("face.registry_cleared", self._on_faces_cleared))
        log.info(
            "FaceService started (cooldown=%.0f min ±%.0f%%, min_absence=%.0f s)",
            self._cooldown_min, self._jitter_pct, self._min_absence_s,
        )

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

    def _on_set_cooldown(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        if "cooldown_min" in payload:
            self._cooldown_min = float(payload["cooldown_min"])
        if "jitter_pct" in payload:
            self._jitter_pct = float(payload["jitter_pct"])
        if "min_absence_s" in payload:
            self._min_absence_s = float(payload["min_absence_s"])
        if "confidence_threshold" in payload:
            self._confidence_threshold = float(payload["confidence_threshold"])
        log.info(
            "FaceService: greeting settings updated — cooldown=%.1f min ±%.0f%%, "
            "min_absence=%.0f s, confidence=%.2f",
            self._cooldown_min, self._jitter_pct, self._min_absence_s,
            self._confidence_threshold,
        )

    def _on_face_deleted(self, _topic, payload) -> None:
        """Purge a single deleted face from in-memory state."""
        if not isinstance(payload, dict):
            return
        face_id = payload.get("face_id")
        if not face_id:
            return
        self._greeted_new_ids.discard(face_id)
        self._prev_face_ids.discard(face_id)
        self._absent_counter.pop(face_id, None)
        log.debug("FaceService: purged face_id %s from in-memory state", face_id[:8])

    def _on_faces_cleared(self, _topic, payload) -> None:
        """Purge deleted faces from in-memory state after a bulk delete."""
        if isinstance(payload, dict) and "face_ids" in payload:
            ids = set(payload["face_ids"])
            self._greeted_new_ids -= ids
            self._prev_face_ids -= ids
            for fid in ids:
                self._absent_counter.pop(fid, None)
        else:
            self._greeted_new_ids.clear()
            self._prev_face_ids.clear()
            self._absent_counter.clear()
        log.debug("FaceService: in-memory state purged on bulk face delete")

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []

        current_face_ids: set[str] = set()

        for face in faces:
            face_id = face.get("face_id")
            name = face.get("name")
            is_new = face.get("is_new", False)
            score = face.get("match_score", 0.0)
            confidence = face.get("confidence", 1.0)

            if not face_id or not name:
                continue

            # Skip low-confidence detections for greetings
            if confidence < self._confidence_threshold:
                continue

            current_face_ids.add(face_id)
            # Reset absent counter for faces that are back
            self._absent_counter.pop(face_id, None)

            self.bus.publish("face.identified", {
                "face_id": face_id,
                "name": name,
                "is_new": is_new,
                "score": score,
            })

            cooldown_s = _jittered_cooldown(self._cooldown_min, self._jitter_pct)
            if is_new:
                self._greet_new(face_id, name)
            elif self._registry.needs_greeting(face_id, cooldown_s, self._min_absence_s):
                self._greet_returning(face_id, name)

        # ── Absence detection ─────────────────────────────────────────────
        # Track faces that newly disappeared this frame
        newly_disappeared = self._prev_face_ids - current_face_ids
        for face_id in newly_disappeared:
            if face_id not in self._absent_counter:
                self._absent_counter[face_id] = 0

        # Increment counter for all tracked absent faces; reset if reappeared
        for face_id in list(self._absent_counter.keys()):
            if face_id in current_face_ids:
                del self._absent_counter[face_id]
            else:
                self._absent_counter[face_id] += 1
                if self._absent_counter[face_id] >= _ABSENCE_DEBOUNCE_FRAMES:
                    self._registry.mark_absent(face_id)
                    del self._absent_counter[face_id]
                    log.debug("FaceService: face %s marked absent (debounced)", face_id[:8])

        self._prev_face_ids = current_face_ids

    def _on_meet(self, _topic, payload) -> None:
        """CLI/web sent 'meet <name>' — name the last seen face."""
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
        self.bus.publish("face.greeted", {
            "face_id": face_id, "name": given_name, "text": text, "event_type": "named",
        })

    # ── Greeting helpers ─────────────────────────────────────────────────

    def _greet_new(self, face_id: str, name: str) -> None:
        if face_id in self._greeted_new_ids:
            return
        self._greeted_new_ids.add(face_id)
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: new-face greeting suppressed — quiet hours")
            return
        self._registry.mark_greeted(face_id)
        phrase = random.choice(_NEW_FACE_PHRASES)
        log.info("Greeting new face %s (%s)", face_id[:8], name)
        self.bus.publish("av.say", {"text": phrase})
        self.bus.publish("face.greeted", {
            "face_id": face_id, "name": name, "text": phrase, "event_type": "new_face",
        })

    def _greet_returning(self, face_id: str, name: str) -> None:
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: returning-face greeting suppressed — quiet hours")
            return
        self._registry.mark_greeted(face_id)
        phrase = self._pick_phrase(name)
        log.info("Re-greeting %s (%s): %r", face_id[:8], name, phrase)
        self.bus.publish("av.say", {"text": phrase})
        self.bus.publish("face.greeted", {
            "face_id": face_id, "name": name, "text": phrase, "event_type": "returning",
        })

    def _pick_phrase(self, name: str) -> str:
        """Pick a time-aware varied greeting, avoiding immediate repeats."""
        bucket = _time_bucket()
        pool = {
            "morning":   _GREET_MORNING,
            "afternoon": _GREET_AFTERNOON,
            "evening":   _GREET_EVENING,
            "night":     _GREET_NIGHT,
        }.get(bucket, _GREET_GENERIC)

        candidates = [p for p in pool if p != self._last_phrase]
        if not candidates:
            candidates = pool
        template = random.choice(candidates)
        self._last_phrase = template
        return template.format(name=name)
