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
import queue
import threading
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


# Sentinel used to wake the worker for shutdown.
_SHUTDOWN = object()


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
        # Single-threaded audio worker: every play action (say/chime/beep)
        # is enqueued here, so they execute strictly in order even when
        # bus events arrive in parallel. Prevents the boot self-test
        # chime from cutting into the in-progress version announcement.
        self._audio_q: "queue.Queue[object]" = queue.Queue()
        self._audio_worker: Optional[threading.Thread] = None

    def on_start(self) -> None:
        if self._audio is None:
            from src.audio.output import AudioOutput
            self._audio = AudioOutput()
        if self._tts is None:
            from src.audio.tts import TextToSpeech
            self._tts = TextToSpeech()
        if self._announcer is None:
            from src.audio.version_announcer import VersionAnnouncer
            self._announcer = VersionAnnouncer(tts=self._tts, audio_output=self._audio)

        self._unsubs.append(self.bus.subscribe("av.say", self._on_say))
        self._unsubs.append(self.bus.subscribe("av.beep", self._on_beep))
        self._unsubs.append(self.bus.subscribe("av.chime", self._on_chime))
        self._unsubs.append(self.bus.subscribe("av.utterance", self._on_utterance))
        self._unsubs.append(
            self.bus.subscribe("av.announce_version", self._on_announce_version)
        )

        # Start the serializing audio worker before any handler can
        # enqueue work into it.
        self._audio_worker = threading.Thread(
            target=self._audio_worker_loop,
            name="av-audio-worker",
            daemon=True,
        )
        self._audio_worker.start()

        log.info(
            "AVService started; audio_ready=%s tts_ready=%s",
            getattr(self._audio, "hardware_ready", False),
            getattr(self._tts, "hardware_ready", False),
        )

        if self._announce_on_start:
            self._enqueue(self._do_announce_startup, label="announce_startup")

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        # Tell worker to drain & exit.
        try:
            self._audio_q.put(_SHUTDOWN)
        except Exception:
            pass
        if self._audio_worker is not None:
            self._audio_worker.join(timeout=5.0)
            self._audio_worker = None
        try:
            if self._audio is not None:
                self._audio.stop()
        except Exception:
            log.exception("audio.stop failed")
        log.info("AVService stopped")

    # ── Worker queue ───────────────────────────────────────────────────

    def _enqueue(self, fn, *, label: str) -> None:
        """Schedule a callable on the single-threaded audio worker."""
        self._audio_q.put((fn, label))

    def _audio_worker_loop(self) -> None:
        while True:
            item = self._audio_q.get()
            if item is _SHUTDOWN:
                self._audio_q.task_done()
                return
            fn, label = item
            try:
                fn()
            except Exception:
                log.exception("audio worker task %r failed", label)
            finally:
                self._audio_q.task_done()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the audio worker has drained its queue. Test hook;
        also useful for callers that want to know audio has fully played
        before continuing. Returns True on idle, False on timeout."""
        import time as _time
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if self._audio_q.unfinished_tasks == 0:
                return True
            _time.sleep(0.005)
        return self._audio_q.unfinished_tasks == 0

    # ── Bus handlers (enqueue only, never block the bus thread) ────────

    def _on_say(self, _topic, payload) -> None:
        text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return
        self._enqueue(lambda t=text: self._do_say(t), label=f"say:{text[:32]}")

    def _on_beep(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        freq = float(payload.get("freq", 880.0))
        duration = float(payload.get("duration", 0.2))
        self._enqueue(lambda: self._do_beep(freq, duration), label="beep")

    def _on_chime(self, _topic, payload) -> None:
        kwargs = {}
        if isinstance(payload, dict):
            if "notes" in payload:
                kwargs["notes"] = tuple(float(n) for n in payload["notes"])
            for k in ("note_duration", "gap", "amplitude"):
                if k in payload:
                    kwargs[k] = float(payload[k])
        self._enqueue(lambda kw=kwargs: self._do_chime(kw), label="chime")

    def _on_utterance(self, _topic, payload) -> None:
        text = (payload or {}).get("text", "") if isinstance(payload, dict) else ""
        if not text:
            return
        self._enqueue(lambda t=text: self._do_utterance(t), label="utterance")

    def _on_announce_version(self, _topic, _payload) -> None:
        self._enqueue(self._do_announce_request, label="announce_request")

    # ── Worker bodies ──────────────────────────────────────────────────

    def _do_say(self, text: str) -> None:
        try:
            self._tts.say(text, output=self._audio)
            self.bus.publish("av.spoke", {"text": text})
        except Exception:
            log.exception("say(%r) failed", text)

    def _do_beep(self, freq: float, duration: float) -> None:
        try:
            self._audio.beep(freq=freq, duration=duration)
        except Exception:
            log.exception("beep failed")

    def _do_chime(self, kwargs: dict) -> None:
        try:
            self._audio.chime(**kwargs)
            self.bus.publish("av.chimed", {})
        except Exception:
            log.exception("chime failed")

    def _do_utterance(self, text: str) -> None:
        try:
            handled = self._announcer.maybe_handle(text)
            if handled:
                from src.core.version import get_version
                self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("utterance handler failed")

    def _do_announce_startup(self) -> None:
        try:
            self._announcer.announce_startup()
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("Startup version announcement failed")

    def _do_announce_request(self) -> None:
        try:
            self._announcer.announce_on_request()
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("announce_version failed")
