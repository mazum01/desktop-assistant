"""
Privacy service — detects nudity in camera frames and commands the head to
look away until the scene is clear.

Topics subscribed
-----------------
privacy.set_enabled   {"enabled": bool}   — runtime enable/disable

Topics published
----------------
privacy.detected      {"explicit": bool, "detections": [...], "ts": float}
privacy.looking_away  {"reason": "explicit_content", "angle_deg": float}
privacy.resuming      {}    — scene cleared, returning to normal behaviour
av.say                {"text": str}   — polite announcement (once per event)

Behaviour
---------
While enabled the service adapts its sampling frequency:
* ``rate_hz`` when a person is present (or while already looking away)
* ``idle_rate_hz`` when no person has been seen recently

When ``require_person`` is true (default), nudity checks run only when a person
was seen recently via ``perception.objects``/``perception.faces``.

On first positive detection it:
  1. Commands the servo to ``look_away_angle_deg`` via ``motion.pan_to``.
  2. Optionally says a polite phrase via TTS.
  3. Suppresses face tracking (``motion.set_enabled`` → False) so the head
     does not follow the person during the privacy window.

After detection clears for ``clear_frames`` consecutive frames it:
  1. Waits ``cooldown_s`` additional seconds.
  2. Re-enables face tracking.
  3. Returns to normal head position (does NOT force-pan; tracking resumes).
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

# Center of the 270° servo range
_SERVO_CENTER = 135.0
# Look-away angle: point camera toward floor/wall
_DEFAULT_LOOK_AWAY_DEG = 45.0
# How many consecutive clear frames before resuming
_DEFAULT_CLEAR_FRAMES = 3


@dataclass
class PrivacyConfig:
    enabled: bool = True
    rate_hz: float = 1.0
    idle_rate_hz: float = 0.25
    threshold: float = 0.6
    look_away_angle_deg: float = _DEFAULT_LOOK_AWAY_DEG
    cooldown_s: float = 10.0
    clear_frames: int = _DEFAULT_CLEAR_FRAMES
    require_person: bool = True
    person_hold_s: float = 8.0
    announce: bool = True
    announce_text: str = "I'll give you some privacy."
    resume_text: str = ""   # empty = no resume announcement


class PrivacyService(Service):
    """Nudity detection + head look-away service."""

    name = "privacy"
    tick_seconds = 999.0  # driven by background thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        config: Optional[PrivacyConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._cfg = config or PrivacyConfig()
        self._enabled: bool = self._cfg.enabled
        self._detector = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._unsubs: list = []

        # State
        self._looking_away: bool = False
        self._clear_streak: int = 0
        self._cooldown_until: float = 0.0
        self._last_person_seen_ts: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_start(self) -> None:
        from src.perception.nudity_detector import NudityDetector
        self._detector = NudityDetector(
            threshold=self._cfg.threshold,
        )
        if self.bus:
            self._unsubs.append(
                self.bus.subscribe("privacy.set_enabled", self._on_set_enabled)
            )
            self._unsubs.append(
                self.bus.subscribe("privacy.set_config", self._on_set_config)
            )
            self._unsubs.append(
                self.bus.subscribe("perception.objects", self._on_objects)
            )
            self._unsubs.append(
                self.bus.subscribe("perception.faces", self._on_faces)
            )
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="privacy-detect", daemon=True
        )
        self._thread.start()
        log.info(
            "PrivacyService started — enabled=%s active_rate=%.2fHz idle_rate=%.2fHz require_person=%s hold=%.1fs threshold=%.2f hw=%s",
            self._enabled,
            self._cfg.rate_hz,
            self._cfg.idle_rate_hz,
            self._cfg.require_person,
            self._cfg.person_hold_s,
            self._cfg.threshold,
            self._detector.hardware_ready,
        )

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _on_set_enabled(self, _topic, payload: dict) -> None:
        self._enabled = bool(payload.get("enabled", True))
        if not self._enabled and self._looking_away:
            self._resume()
        log.info("PrivacyService: enabled=%s", self._enabled)

    def _on_set_config(self, _topic, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        if "enabled" in payload:
            self._enabled = bool(payload["enabled"])
        if "rate_hz" in payload:
            self._cfg.rate_hz = max(0.1, float(payload["rate_hz"]))
        if "idle_rate_hz" in payload:
            self._cfg.idle_rate_hz = max(0.05, float(payload["idle_rate_hz"]))
        if "threshold" in payload:
            self._cfg.threshold = max(0.0, min(1.0, float(payload["threshold"])))
            if self._detector is not None:
                try:
                    from src.perception.nudity_detector import NudityDetector
                    self._detector = NudityDetector(threshold=self._cfg.threshold)
                except Exception:
                    log.debug("PrivacyService: detector threshold refresh failed", exc_info=True)
        if "look_away_angle_deg" in payload:
            self._cfg.look_away_angle_deg = float(payload["look_away_angle_deg"])
        if "cooldown_s" in payload:
            self._cfg.cooldown_s = max(0.0, float(payload["cooldown_s"]))
        if "clear_frames" in payload:
            self._cfg.clear_frames = max(1, int(payload["clear_frames"]))
        if "require_person" in payload:
            self._cfg.require_person = bool(payload["require_person"])
        if "person_hold_s" in payload:
            self._cfg.person_hold_s = max(0.5, float(payload["person_hold_s"]))
        if "announce" in payload:
            self._cfg.announce = bool(payload["announce"])
        if "announce_text" in payload:
            self._cfg.announce_text = str(payload["announce_text"])
        if "resume_text" in payload:
            self._cfg.resume_text = str(payload["resume_text"])
        if not self._enabled and self._looking_away:
            self._resume()
        log.info(
            "PrivacyService: config updated enabled=%s active_rate=%.2f idle_rate=%.2f require_person=%s hold=%.1fs threshold=%.2f look=%.1f cooldown=%.1f clear=%d announce=%s",
            self._enabled,
            self._cfg.rate_hz,
            self._cfg.idle_rate_hz,
            self._cfg.require_person,
            self._cfg.person_hold_s,
            self._cfg.threshold,
            self._cfg.look_away_angle_deg,
            self._cfg.cooldown_s,
            self._cfg.clear_frames,
            self._cfg.announce,
        )

    @property
    def hardware_ready(self) -> bool:
        return self._detector is not None and self._detector.hardware_ready

    def _effective_rate_hz(self, now: float) -> float:
        active = max(0.1, float(self._cfg.rate_hz))
        idle = min(active, max(0.05, float(self._cfg.idle_rate_hz)))
        if self._looking_away or self._has_recent_person(now):
            return active
        return idle

    def _has_recent_person(self, now: float) -> bool:
        if self._last_person_seen_ts <= 0:
            return False
        return (now - self._last_person_seen_ts) <= self._cfg.person_hold_s

    def _should_run_detection(self, now: float) -> bool:
        if self._looking_away:
            return True
        if not self._cfg.require_person:
            return True
        return self._has_recent_person(now)

    def _on_objects(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        objects = payload.get("objects") or []
        if not isinstance(objects, list):
            return
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            label = str(obj.get("label") or obj.get("name") or "").strip().lower()
            if label == "person":
                self._last_person_seen_ts = time.monotonic()
                return

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        faces = payload.get("faces") or []
        if isinstance(faces, list) and faces:
            self._last_person_seen_ts = time.monotonic()

    # ── Detection loop ───────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            if not self._enabled:
                self._stop_event.wait(timeout=0.5)
                continue

            t0 = time.monotonic()
            try:
                self._check_frame()
            except Exception:
                log.debug("PrivacyService: frame check error", exc_info=True)
            elapsed = time.monotonic() - t0
            interval = max(0.2, 1.0 / self._effective_rate_hz(time.monotonic()))
            remaining = interval - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    def _check_frame(self) -> None:
        now = time.monotonic()
        if not self._should_run_detection(now):
            return
        if self._vision_svc is None:
            return
        try:
            frame = self._vision_svc.latest_frame()
        except Exception:
            return
        if frame is None:
            return

        explicit, detections = self._detector.is_explicit(frame)

        if self.bus:
            self.bus.publish("privacy.detected", {
                "explicit": explicit,
                "detections": detections,
                "ts": time.time(),
            })

        now = time.monotonic()
        if explicit:
            self._clear_streak = 0
            if not self._looking_away:
                self._look_away()
        else:
            if self._looking_away:
                self._clear_streak += 1
                if self._clear_streak >= self._cfg.clear_frames:
                    if now >= self._cooldown_until:
                        self._resume()
                    elif self._cooldown_until - now > 0.1:
                        # Already in cooldown — let the timer expire
                        pass
                    else:
                        self._cooldown_until = now + self._cfg.cooldown_s
            else:
                self._clear_streak = 0

    def _look_away(self) -> None:
        self._looking_away = True
        self._clear_streak = 0
        self._cooldown_until = time.monotonic() + self._cfg.cooldown_s
        angle = self._cfg.look_away_angle_deg

        log.info("PrivacyService: explicit content detected — panning to %.1f°", angle)

        if self.bus:
            # Disable face tracking so head stays pointed away
            self.bus.publish("motion.set_enabled", {"enabled": False})
            # Pan away
            self.bus.publish("motion.pan_to", {
                "angle": angle,
                "move_time_ms": 500,
            })
            # Publish event
            self.bus.publish("privacy.looking_away", {
                "reason": "explicit_content",
                "angle_deg": angle,
            })
            # Announce once
            if self._cfg.announce and self._cfg.announce_text:
                self.bus.publish("av.say", {"text": self._cfg.announce_text})

    def _resume(self) -> None:
        self._looking_away = False
        self._clear_streak = 0
        log.info("PrivacyService: scene clear — resuming normal operation")

        if self.bus:
            # Re-enable motion (tracking service will take back control)
            self.bus.publish("motion.set_enabled", {"enabled": True})
            self.bus.publish("privacy.resuming", {})
            if self._cfg.resume_text:
                self.bus.publish("av.say", {"text": self._cfg.resume_text})
