"""
AV (Audio/Video) service.

Owns: AudioOutput, TextToSpeech, VersionAnnouncer.
(Camera lives here too in future, but for now is opened on demand.)

Topics subscribed:
    av.say                {"text": str}            — speak the given text
    av.beep               {"freq": float, "duration": float}  (optional)
    av.chime              {} or {notes/note_duration/gap/amplitude} —
                           plays the boot arpeggio (C5-E5-G5 by default)
    av.utterance          {"text": str}            — user said something;
                                                     handle version queries
    av.announce_version   None                     — speak the current version

Topics published:
    av.spoke              {"text": str}
    av.chimed             {}
    av.version_announced  {"version": str}
"""

from __future__ import annotations

import logging
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class AVService(Service):
    name = "av"
    tick_seconds = 5.0  # mostly event-driven; tick is a heartbeat

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        audio_output=None,
        tts=None,
        announcer=None,
        announce_on_start: bool = True,
    ) -> None:
        super().__init__(bus=bus)
        self._audio = audio_output
        self._tts = tts
        self._announcer = announcer
        self._announce_on_start = announce_on_start
        self._unsubs = []

    def on_start(self) -> None:
        if self._audio is None:
            from src.audio.output import AudioOutput
            self._audio = AudioOutput()
        if self._tts is None:
            from src.audio.tts import TextToSpeech
            self._tts = TextToSpeech()
        if self._announcer is None:
            from src.audio.version_announcer import VersionAnnouncer
            self._announcer = VersionAnnouncer(tts=self._tts, output=self._audio)

        self._unsubs.append(self.bus.subscribe("av.say", self._on_say))
        self._unsubs.append(self.bus.subscribe("av.beep", self._on_beep))
        self._unsubs.append(self.bus.subscribe("av.chime", self._on_chime))
        self._unsubs.append(self.bus.subscribe("av.utterance", self._on_utterance))
        self._unsubs.append(
            self.bus.subscribe("av.announce_version", self._on_announce_version)
        )

        log.info(
            "AVService started; audio_ready=%s tts_ready=%s",
            getattr(self._audio, "hardware_ready", False),
            getattr(self._tts, "hardware_ready", False),
        )

        if self._announce_on_start:
            try:
                self._announcer.announce_startup()
                from src.core.version import get_version
                self.bus.publish("av.version_announced", {"version": get_version()})
            except Exception:
                log.exception("Startup version announcement failed")

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        try:
            if self._audio is not None:
                self._audio.stop()
        except Exception:
            log.exception("audio.stop failed")
        log.info("AVService stopped")

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_say(self, _topic, payload) -> None:
        text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return
        try:
            self._tts.say(text, output=self._audio)
            self.bus.publish("av.spoke", {"text": text})
        except Exception:
            log.exception("say(%r) failed", text)

    def _on_beep(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        try:
            self._audio.beep(
                freq=float(payload.get("freq", 880.0)),
                duration=float(payload.get("duration", 0.2)),
            )
        except Exception:
            log.exception("beep failed")

    def _on_chime(self, _topic, payload) -> None:
        kwargs = {}
        if isinstance(payload, dict):
            if "notes" in payload:
                kwargs["notes"] = tuple(float(n) for n in payload["notes"])
            for k in ("note_duration", "gap", "amplitude"):
                if k in payload:
                    kwargs[k] = float(payload[k])
        try:
            self._audio.chime(**kwargs)
            self.bus.publish("av.chimed", {})
        except Exception:
            log.exception("chime failed")

    def _on_utterance(self, _topic, payload) -> None:
        text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return
        try:
            handled = self._announcer.maybe_handle(text)
            if handled:
                from src.core.version import get_version
                self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("utterance handler failed")

    def _on_announce_version(self, _topic, _payload) -> None:
        try:
            self._announcer.announce_on_request()
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("announce_version failed")
