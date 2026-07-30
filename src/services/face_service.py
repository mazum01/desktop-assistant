"""
Face greeting service.

Listens for identified faces on ``perception.faces``, speaks greetings via
``av.say``, and handles the ``face.meet`` bus event for name assignment (sent
by the CLI ``desktop-assistant meet <name>`` command).

Greeting rules
--------------
* **Known face** (is_new=False): re-greet only if ``needs_greeting()`` is True —
  face was absent ≥ *min_absence_s*, cooldown (±jitter) has elapsed since last greet.
* **New/guest face** (is_new=True): intro only after the face has been continuously
  present for *guest_intro_delay_min* minutes (default 2). If it disappears before
  the delay expires the timer resets. This prevents VERA from greeting someone who
  just walked past. Once the delay elapses VERA introduces herself once per session.
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
face.greeted        ``{face_id, name, text, event_type}``  — Telegram / audit
                    event_type: "new" | "returning" | "returning_corrected"
"""

from __future__ import annotations

import logging
import json
import os
import random
import shutil
import subprocess
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
    "Oh hey, I don't think we've met! I'm VERA. What's your name?",
    "Whoa, a new face! Hi there, I'm VERA. And you are?",
    "Hello! Don't think I know you yet. I'm VERA. Mind introducing yourself?",
    "Hey! I'm VERA. I'd love to know your name. What should I call you?",
    "Well, hello there! I'm VERA. I haven't had the pleasure. What's your name?",
]

# Default cooldown: 30 minutes base, ±25% jitter
_DEFAULT_COOLDOWN_MIN       = 30.0
_DEFAULT_JITTER_PCT         = 25.0
_DEFAULT_MIN_ABSENCE_S      = 30.0
_DEFAULT_CONFIDENCE         = 0.5
_DEFAULT_GUEST_INTRO_DELAY  = 2.0  # minutes before greeting an unrecognized face

# Absence debounce: face must be missing this many consecutive frames
_ABSENCE_DEBOUNCE_FRAMES = 3

# ── Contrite correction phrases ───────────────────────────────────────────────
# Spoken when stabilisation reveals the initial identity guess was wrong.
# {name} = the correct (committed) identity.

_CONTRITE_PHRASES = [
    "Oh wait, I got that wrong — {name}! Sorry about the mix-up, great to see you!",
    "Hold on, I need to apologise. That's {name}! Welcome, and sorry for the confusion.",
    "I'm sorry, I think I had that wrong. {name}! My apologies, glad you're here.",
    "Oops — I mixed you up with someone else. {name}! Sorry about that, good to see you!",
]

# ── OpenClaw greeting generation defaults ─────────────────────────────────────
_DEFAULT_OPENCLAW_GREETINGS_ENABLED = False
_DEFAULT_OPENCLAW_GREETING_MODEL = "anthropic/claude-sonnet-4-6"
_DEFAULT_OPENCLAW_GREETING_TIMEOUT_S = 45.0
_DEFAULT_OPENCLAW_CLI_PATH = ""
_NVM_OPENCLAW_FALLBACK = "/home/starter/.nvm/versions/node/v24.15.0/bin/openclaw"


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
        guest_intro_delay_min: float = _DEFAULT_GUEST_INTRO_DELAY,
        openclaw_greetings_enabled: bool = _DEFAULT_OPENCLAW_GREETINGS_ENABLED,
        openclaw_greeting_model: str = _DEFAULT_OPENCLAW_GREETING_MODEL,
        openclaw_greeting_timeout_s: float = _DEFAULT_OPENCLAW_GREETING_TIMEOUT_S,
        openclaw_cli_path: str = _DEFAULT_OPENCLAW_CLI_PATH,
        anthropic_enabled: bool = True,
    ) -> None:
        super().__init__(bus=bus)
        self._registry = registry
        self._cooldown_min = greeting_cooldown_min
        self._jitter_pct = greeting_cooldown_jitter_pct
        self._min_absence_s = min_absence_s
        self._confidence_threshold = confidence_threshold
        self._quiet_hours = quiet_hours
        self._guest_intro_delay_s = guest_intro_delay_min * 60.0
        self._unsubs: list = []
        self._last_phrase: Optional[str] = None
        self._greeted_new_ids: set[str] = set()   # session guard: intro each new face once
        self._prev_face_ids: set[str] = set()     # face_ids in previous frame
        self._absent_counter: dict[str, int] = {} # face_id → consecutive absent-frame count
        # face_id → monotonic time when first seen as unrecognized (Guest)
        self._guest_first_seen: dict[str, float] = {}
        self._openclaw_greetings_enabled = bool(openclaw_greetings_enabled)
        self._openclaw_greeting_model = (openclaw_greeting_model or "").strip()
        self._openclaw_greeting_timeout_s = max(0.5, float(openclaw_greeting_timeout_s))
        self._openclaw_cli_path_cfg = str(openclaw_cli_path or "").strip()
        self._openclaw_cli_path: str | None = None
        self._anthropic_enabled: bool = bool(anthropic_enabled)

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
        self._unsubs.append(self.bus.subscribe("face.refresh", self._on_face_refresh))
        self._unsubs.append(
            self.bus.subscribe("anthropic.set_enabled", self._on_set_anthropic_enabled)
        )
        self._openclaw_cli_path = self._resolve_openclaw_cli_path()
        if self._openclaw_greetings_enabled and not self._openclaw_cli_path:
            log.warning(
                "FaceService: OpenClaw greetings enabled but CLI not found; using static greetings. "
                "Set face_recognition.openclaw_cli_path in assistant.yaml."
            )
        log.info(
            "FaceService started (cooldown=%.0f min ±%.0f%%, min_absence=%.0f s, "
            "guest_intro_delay=%.1f min, openclaw_greetings=%s)",
            self._cooldown_min, self._jitter_pct, self._min_absence_s,
            self._guest_intro_delay_s / 60.0,
            "on" if self._openclaw_greetings_enabled else "off",
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

    def _on_set_anthropic_enabled(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "enabled" in payload:
            self._anthropic_enabled = bool(payload["enabled"])
            log.info(
                "FaceService: Anthropic API %s",
                "enabled" if self._anthropic_enabled else "disabled",
            )

    def _uses_anthropic_model(self) -> bool:
        """True if the configured OpenClaw greeting model routes to Anthropic/Claude."""
        model = self._openclaw_greeting_model.lower()
        return "anthropic" in model or "claude" in model

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
        self._guest_first_seen.pop(face_id, None)
        log.debug("FaceService: purged face_id %s from in-memory state", face_id[:8])

    def _on_faces_cleared(self, _topic, payload) -> None:
        """Purge deleted faces from in-memory state after a bulk delete."""
        if isinstance(payload, dict) and "face_ids" in payload:
            ids = set(payload["face_ids"])
            self._greeted_new_ids -= ids
            self._prev_face_ids -= ids
            for fid in ids:
                self._absent_counter.pop(fid, None)
                self._guest_first_seen.pop(fid, None)
        else:
            self._greeted_new_ids.clear()
            self._prev_face_ids.clear()
            self._absent_counter.clear()
            self._guest_first_seen.clear()
        log.debug("FaceService: in-memory state purged on bulk face delete")

    def _on_face_refresh(self, _topic, _payload) -> None:
        """Reload the embedding cache from DB and reset tracking state so faces are re-identified."""
        if self._registry is not None:
            self._registry.reload()
        self._greeted_new_ids.clear()
        self._prev_face_ids.clear()
        self._absent_counter.clear()
        self._guest_first_seen.clear()
        log.info("FaceService: embedding cache reloaded and tracking state reset")

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

            # Skip faces that are still in the stabilisation window —
            # we don't know the confirmed identity yet, so no greeting.
            if face.get("is_stabilizing", False):
                continue

            stabilization_changed = face.get("stabilization_changed", False)
            initial_name = face.get("initial_name")

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
                self._maybe_greet_new(face_id, name)
            else:
                # Face was recognized — clear any pending guest intro timer
                self._guest_first_seen.pop(face_id, None)
                if self._registry.needs_greeting(face_id, cooldown_s, self._min_absence_s):
                    self._greet_returning(face_id, name,
                                          stabilization_changed=stabilization_changed,
                                          initial_name=initial_name)

        # ── Absence detection ─────────────────────────────────────────────
        # Track faces that newly disappeared this frame
        newly_disappeared = self._prev_face_ids - current_face_ids
        for face_id in newly_disappeared:
            if face_id not in self._absent_counter:
                self._absent_counter[face_id] = 0
            # Reset the guest intro timer — face left before the delay expired
            if face_id in self._guest_first_seen:
                log.debug(
                    "FaceService: guest %s left before intro delay — resetting timer",
                    face_id[:8],
                )
                del self._guest_first_seen[face_id]

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
        """CLI/web sent 'meet <name>' — name the last seen face.

        When ``face_id`` is present in the payload (web-UI rename), use it
        directly so we never accidentally rename whoever happens to be in frame.
        Fall back to the current face only for CLI usage (no face_id supplied).
        """
        if not isinstance(payload, dict):
            return
        given_name = (payload.get("name") or "").strip()
        if not given_name:
            log.warning("face.meet received empty name")
            return
        if self._registry is None:
            return

        # Web-UI rename supplies the explicit face_id; CLI does not.
        face_id = payload.get("face_id") or self._registry.get_current_face_id()
        if face_id is None:
            log.warning("face.meet: no faces in registry yet")
            self.bus.publish("av.say", {"text": "I haven't seen anyone yet to name."})
            return

        old_name = (self._registry.get_face(face_id) or {}).get("name", "")
        # Skip redundant DB write when the web API already called set_name.
        if old_name != given_name:
            self._registry.set_name(face_id, given_name)
        self._registry.mark_greeted(face_id)
        text = f"Nice to meet you, {given_name}! I'll remember you."
        log.info("Named face %s: %r → %r", face_id[:8], old_name, given_name)
        self.bus.publish("av.say", {"text": text})

    # ── Greeting helpers ─────────────────────────────────────────────────

    def _maybe_greet_new(self, face_id: str, name: str) -> None:
        """Greet a new/guest face only after it has been present for the intro delay.

        - First sighting: record timestamp. If delay is 0, fire immediately.
        - Face leaves before delay: timer reset by absence handler.
        - Delay elapsed and still present: fire the intro greeting.
        - Already greeted this session: no-op.
        """
        if face_id in self._greeted_new_ids:
            return

        now = time.monotonic()
        if face_id not in self._guest_first_seen:
            self._guest_first_seen[face_id] = now
            if self._guest_intro_delay_s <= 0:
                # Zero delay — greet immediately on first sighting
                del self._guest_first_seen[face_id]
                self._greet_new(face_id, name)
            else:
                log.debug(
                    "FaceService: new face %s (%s) — starting %.1f-min intro delay",
                    face_id[:8], name, self._guest_intro_delay_s / 60.0,
                )
            return

        elapsed = now - self._guest_first_seen[face_id]
        if elapsed < self._guest_intro_delay_s:
            # Still within the delay window — stay silent
            return

        # Delay expired and face is still present — time to introduce
        del self._guest_first_seen[face_id]
        self._greet_new(face_id, name)

    def _greet_new(self, face_id: str, name: str) -> None:
        if face_id in self._greeted_new_ids:
            return
        self._greeted_new_ids.add(face_id)
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: new-face greeting suppressed — quiet hours")
            return
        self._registry.mark_greeted(face_id)
        phrase = random.choice(_NEW_FACE_PHRASES)
        source = "static"
        generated = self._generate_openclaw_greeting(
            event_type="new",
            name=name,
        )
        if generated:
            phrase = generated
            source = "openclaw"
        log.info("Greeting new face %s (%s) via %s", face_id[:8], name, source)
        self.bus.publish("av.say", {"text": phrase})
        self.bus.publish("face.greeted", {
            "face_id": face_id, "name": name, "text": phrase, "event_type": "new",
        })

    def _greet_returning(
        self,
        face_id: str,
        name: str,
        stabilization_changed: bool = False,
        initial_name: str | None = None,
    ) -> None:
        if self._quiet_hours and self._quiet_hours.is_quiet():
            log.debug("FaceService: returning-face greeting suppressed — quiet hours")
            return
        self._registry.mark_greeted(face_id)
        if stabilization_changed and initial_name:
            phrase = random.choice(_CONTRITE_PHRASES).format(name=name)
            event_type = "returning_corrected"
            source = "static"
            log.info(
                "Contrite re-greeting %s (%s, was %r) via %s: %r",
                face_id[:8], name, initial_name, source, phrase,
            )
        else:
            phrase = self._pick_phrase(name)
            source = "static"
            generated = self._generate_openclaw_greeting(
                event_type="returning",
                name=name,
            )
            if generated:
                phrase = generated
                source = "openclaw"
            event_type = "returning"
            log.info("Re-greeting %s (%s) via %s: %r", face_id[:8], name, source, phrase)
        self.bus.publish("av.say", {"text": phrase})
        self.bus.publish("face.greeted", {
            "face_id": face_id, "name": name, "text": phrase, "event_type": event_type,
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

    def _generate_openclaw_greeting(self, event_type: str, name: str) -> str | None:
        """Generate a natural greeting via OpenClaw; return None on any failure."""
        if not self._openclaw_greetings_enabled:
            return None
        if not self._openclaw_cli_path:
            return None
        if not self._anthropic_enabled and self._uses_anthropic_model():
            log.debug(
                "FaceService: Anthropic API disabled via config — skipping OpenClaw "
                "greeting (model=%r)", self._openclaw_greeting_model,
            )
            return None

        if event_type == "new":
            prompt = (
                "You are VERA, a friendly home desktop assistant. "
                "Write one short, natural spoken greeting for a new person you don't know yet. "
                "Do not ask multiple questions. Keep it warm and conversational. "
                "Use 1 sentence, max 16 words. Mention you are VERA."
            )
        else:
            bucket = _time_bucket()
            prompt = (
                f"You are VERA, greeting {name} who just returned. "
                f"Write one natural {bucket}-appropriate spoken greeting. "
                "Use exactly one sentence, max 14 words, and include the person's name."
            )

        cmd = [
            self._openclaw_cli_path,
            "infer",
            "model",
            "run",
            "--gateway",
            "--json",
            "--thinking",
            "off",
            "--prompt",
            prompt,
        ]
        if self._openclaw_greeting_model:
            cmd.extend(["--model", self._openclaw_greeting_model])

        try:
            env = self._openclaw_env()
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self._openclaw_greeting_timeout_s,
                env=env,
            )
        except OSError:
            log.debug("FaceService: OpenClaw greeting exec failed (cli missing or not executable)")
            return None
        except subprocess.TimeoutExpired:
            log.debug(
                "FaceService: OpenClaw greeting timed out after %.1fs; falling back to static phrase",
                self._openclaw_greeting_timeout_s,
            )
            return None

        if proc.returncode != 0:
            log.warning(
                "FaceService: OpenClaw greeting failed: rc=%s stderr=%s",
                proc.returncode,
                (proc.stderr or "").strip(),
            )
            return None

        try:
            payload = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            log.debug("FaceService: OpenClaw greeting returned non-JSON output")
            return None

        outputs = payload.get("outputs")
        if not isinstance(outputs, list) or not outputs:
            return None
        first = outputs[0] if isinstance(outputs[0], dict) else {}
        text = str(first.get("text") or "").strip()
        if not text:
            return None
        text = " ".join(text.split())
        text = text.strip("\"'")
        if len(text) > 180:
            text = text[:180].rstrip(" ,.;:!?")
        if event_type == "returning" and name.lower() not in text.lower():
            text = f"{name}! {text}"
        return text or None

    def _openclaw_env(self) -> dict[str, str]:
        """Build env for OpenClaw subprocess with daemon-safe PATH."""
        env = dict(os.environ)
        if self._openclaw_cli_path:
            cli_dir = os.path.dirname(self._openclaw_cli_path)
            if cli_dir:
                current = env.get("PATH", "")
                parts = current.split(os.pathsep) if current else []
                if cli_dir not in parts:
                    env["PATH"] = f"{cli_dir}{os.pathsep}{current}" if current else cli_dir
        return env

    def _resolve_openclaw_cli_path(self) -> str | None:
        """Resolve the OpenClaw CLI path for daemon-safe execution."""
        if self._openclaw_cli_path_cfg:
            expanded = os.path.expanduser(self._openclaw_cli_path_cfg)
            found = shutil.which(expanded)
            return found or expanded
        found = shutil.which("openclaw")
        if found:
            return found
        if shutil.which(_NVM_OPENCLAW_FALLBACK):
            return _NVM_OPENCLAW_FALLBACK
        return None
