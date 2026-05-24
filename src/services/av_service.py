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
    av.set_eq_preset      {"preset": str}          — switch named EQ preset
    av.set_custom_eq      {"bands": [...]}         — set user-defined EQ bands;
                           each band: {"hz": float, "gain_db": float, "q": float}
    av.record             {"seconds": float, "path": str?} — record mic audio to WAV
    av.play_recording     {"path": str?}           — play latest or specified WAV

Topics published:
    av.spoke              {"text": str}
    av.chimed             {}
    av.version_announced  {"version": str}
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import queue
import threading
import time
import wave
from pathlib import Path
from typing import Any, Optional, cast

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

_SHUTDOWN = object()

_STATE_DIR   = Path.home() / ".config" / "desktop-assistant"
_EQ_STATE_FILE       = _STATE_DIR / "eq_preset.txt"
_CUSTOM_EQ_STATE_FILE = _STATE_DIR / "custom_eq.json"
_RECORDINGS_DIR = Path.home() / "Pictures" / "vera" / "recordings"


class AVService(Service):
    name = "av"
    tick_seconds = 5.0  # mostly event-driven; tick is a heartbeat

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        audio_output=None,
        tts=None,
        audio_input=None,
        announcer=None,
        announce_on_start: bool = True,
    ) -> None:
        super().__init__(bus=bus)
        self._audio = audio_output
        self._tts = tts
        self._mic = audio_input
        self._announcer = announcer
        self._announce_on_start = announce_on_start
        self._unsubs = []
        # Injected reference to AudioCaptureService for conflict-free recording.
        # When set, _do_record_clip collects from this service's running stream
        # instead of opening a competing PortAudio input stream via sd.rec().
        self._capture_svc: Optional[Any] = None
        # Single-threaded audio worker: every play action (say/chime/beep)
        # is enqueued here, so they execute strictly in order even when
        # bus events arrive in parallel. Prevents the boot self-test
        # chime from cutting into the in-progress version announcement.
        self._audio_q: "queue.Queue[tuple[Any, str] | object]" = queue.Queue()
        self._audio_worker: Optional[threading.Thread] = None
        self._last_recording_path: Optional[Path] = None
        # Single-threaded synthesis executor: TTS synthesis is CPU-heavy
        # (~17s on Pi 5). All synthesis runs here so the audio worker only
        # blocks during actual playback (~3-5s), keeping recording responsive.
        self._synth_executor: concurrent.futures.ThreadPoolExecutor = (
            concurrent.futures.ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="tts-synth"
            )
        )

    def set_capture_service(self, svc: Any) -> None:
        """Inject the AudioCaptureService so _do_record_clip can collect from
        its already-running PortAudio stream instead of opening a second one."""
        self._capture_svc = svc

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
        self._unsubs.append(self.bus.subscribe("av.set_eq_preset",  self._on_set_eq_preset))
        self._unsubs.append(self.bus.subscribe("av.set_custom_eq",  self._on_set_custom_eq))
        self._unsubs.append(self.bus.subscribe("av.record", self._on_record))
        self._unsubs.append(self.bus.subscribe("av.play_recording", self._on_play_recording))

        # Start the serializing audio worker before any handler can
        # enqueue work into it.
        self._audio_worker = threading.Thread(
            target=self._audio_worker_loop,
            name="av-audio-worker",
            daemon=True,
        )
        self._audio_worker.start()

        # Pre-warm TTS in background. The announcement is enqueued ONLY AFTER
        # the model loads so the audio worker stays free for recording requests
        # during the ~22 s Piper ONNX cold-start.
        if self._tts is not None and hasattr(self._tts, "prewarm"):
            threading.Thread(
                target=self._prewarm_tts,
                name="tts-prewarm",
                daemon=True,
            ).start()
            # _prewarm_tts enqueues the announcement when the model is ready
        elif self._announce_on_start:
            self._enqueue(self._do_announce_startup, label="announce_startup")

        log.info(
            "AVService started; audio_ready=%s tts_ready=%s",
            getattr(self._audio, "hardware_ready", False),
            getattr(self._tts, "hardware_ready", False),
        )

        # Restore persisted EQ state
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        if _CUSTOM_EQ_STATE_FILE.exists():
            try:
                bands = json.loads(_CUSTOM_EQ_STATE_FILE.read_text())
                if self._audio is not None:
                    self._audio.set_custom_eq_bands(bands)
                log.info("AVService: restored custom EQ (%d band(s))", len(bands))
            except Exception as exc:
                log.warning("AVService: failed to restore custom EQ: %s", exc)
        elif _EQ_STATE_FILE.exists():
            try:
                preset = _EQ_STATE_FILE.read_text().strip()
                if preset and preset != "custom" and self._audio is not None:
                    self._audio.set_eq_preset(preset)
                log.info("AVService: restored EQ preset %r", preset)
            except Exception as exc:
                log.warning("AVService: failed to restore EQ preset: %s", exc)

        # Restore PipeWire system EQ (filter-chain config already on disk from
        # last session — just re-elect the EQ sink as default without restarting).
        threading.Thread(target=self._restore_pipewire_eq, daemon=True,
                         name="pw-eq-restore").start()

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        # Shut down synthesis executor (cancel pending synthesis tasks).
        try:
            self._synth_executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
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
            if not isinstance(item, tuple) or len(item) != 2:
                self._audio_q.task_done()
                continue
            fn, label = item
            try:
                fn()
            except Exception:
                log.exception("audio worker task %r failed", label)
            finally:
                self._audio_q.task_done()

    def _enqueue_wait(self, fn, *, label: str, timeout: float = 90.0):
        """Run *fn* on the audio worker and wait for its return value."""
        result_q: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

        def _wrapped():
            try:
                result_q.put(("ok", fn()))
            except Exception as exc:
                result_q.put(("err", exc))

        self._enqueue(_wrapped, label=label)
        try:
            status, value = result_q.get(timeout=timeout)
        except queue.Empty as exc:
            raise TimeoutError(f"audio task timed out: {label}") from exc
        if status == "err":
            raise value  # type: ignore[misc]
        return value

    def record_clip(self, seconds: float = 5.0, path: str | None = None) -> dict:
        """Record audio from the microphone and write it to a WAV file."""
        return cast(dict, self._enqueue_wait(
            lambda: self._do_record_clip(seconds=seconds, path=path),
            label="record_clip",
            timeout=max(30.0, float(seconds) + 30.0),
        ))

    def play_recording(self, path: str | None = None) -> dict:
        """Play the latest recording or an explicit WAV path."""
        return cast(dict, self._enqueue_wait(
            lambda: self._do_play_recording(path=path),
            label="play_recording",
            timeout=90.0,
        ))

    def tts_duration_rpc(self, text: str) -> float:
        """Return exact TTS render duration for *text* in seconds.
        Safe to call from any thread; TTS is initialised lazily if needed."""
        if self._tts is None:
            from src.audio.tts import TextToSpeech
            tts = TextToSpeech()
        else:
            tts = self._tts
        return tts.render_duration(text)

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the synth executor and audio worker have both drained.

        Test hook; also useful for callers that want to know audio has fully
        played before continuing. Returns True on idle, False on timeout."""
        import time as _time
        deadline = _time.monotonic() + timeout

        # Wait for synth executor to drain first. Any synthesis tasks queued
        # before this barrier will have enqueued their playback tasks by the
        # time the barrier resolves.
        barrier: "concurrent.futures.Future[bool]" = concurrent.futures.Future()
        self._synth_executor.submit(lambda: barrier.set_result(True))
        remaining = deadline - _time.monotonic()
        try:
            barrier.result(timeout=max(0.0, remaining))
        except Exception:
            return False

        # Now wait for the audio worker queue to drain.
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
        request_id = (payload or {}).get("request_id") if isinstance(payload, dict) else None
        self._submit_say(text, request_id=request_id)

    def _submit_say(self, text: str, request_id: str | None = None) -> None:
        """Synthesize *text* in the background (synth executor), then enqueue
        playback-only to the audio worker. The audio worker stays free for
        recordings while 17+ second Piper synthesis runs in parallel."""
        if self._tts is None:
            return

        def _synth_then_enqueue() -> None:
            try:
                samples, sr = self._tts.render(text)
            except Exception:
                log.exception("TTS synthesis failed for %r", text)
                return
            self._enqueue(
                lambda s=samples, r=sr, t=text, rid=request_id: self._do_play_samples(
                    s, r, t, rid
                ),
                label=f"say:{text[:32]}",
            )

        self._synth_executor.submit(_synth_then_enqueue)

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

    def _on_record(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        seconds = payload.get("seconds", 5.0)
        path = payload.get("path")
        self._enqueue(
            lambda s=seconds, p=path: self._do_record_clip(seconds=s, path=p),
            label="record_clip",
        )

    def _on_play_recording(self, _topic, payload) -> None:
        path = payload.get("path") if isinstance(payload, dict) else None
        self._enqueue(
            lambda p=path: self._do_play_recording(path=p),
            label="play_recording",
        )

    def _on_set_eq_preset(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        preset = payload.get("preset", "")
        if not preset or preset == "custom":
            return
        if self._audio is not None:
            self._audio.set_eq_preset(preset)
        # Persist
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _EQ_STATE_FILE.write_text(preset)
            # Clear custom EQ file — named preset takes over
            if _CUSTOM_EQ_STATE_FILE.exists():
                _CUSTOM_EQ_STATE_FILE.unlink()
        except Exception as exc:
            log.warning("AVService: failed to persist EQ preset: %s", exc)
        log.info("AVService: EQ preset → %r", preset)
        # Apply system-wide via PipeWire (runs in background — takes ~1-2 s).
        threading.Thread(target=self._apply_pipewire_preset, args=(preset,),
                         daemon=True, name="pw-eq").start()

    def _on_set_custom_eq(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        bands = payload.get("bands")
        if not isinstance(bands, list):
            return
        if self._audio is not None:
            self._audio.set_custom_eq_bands(bands)
        # Persist
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _CUSTOM_EQ_STATE_FILE.write_text(json.dumps(bands))
            # Write "custom" to the preset file so on_start picks correct branch
            _EQ_STATE_FILE.write_text("custom")
        except Exception as exc:
            log.warning("AVService: failed to persist custom EQ: %s", exc)
        log.info("AVService: custom EQ set (%d band(s))", len(bands))
        # Apply system-wide via PipeWire.
        threading.Thread(target=self._apply_pipewire_custom, args=(bands,),
                         daemon=True, name="pw-eq-custom").start()

    def _apply_pipewire_preset(self, preset: str) -> None:
        try:
            from src.audio import pipewire_eq
            if pipewire_eq.apply_preset(preset):
                # PipeWire handles EQ for all audio — disable Python biquad on TTS
                # to avoid double-processing.
                if self._audio is not None:
                    self._audio.set_eq_preset("flat")
            else:
                log.info("AVService: PipeWire EQ unavailable; using software EQ for TTS")
        except Exception as exc:
            log.warning("AVService: PipeWire EQ apply failed: %s", exc)

    def _apply_pipewire_custom(self, bands: list) -> None:
        try:
            from src.audio import pipewire_eq
            if pipewire_eq.apply_custom_bands(bands):
                if self._audio is not None:
                    self._audio.set_eq_preset("flat")
            else:
                log.info("AVService: PipeWire EQ unavailable; using software EQ for TTS")
        except Exception as exc:
            log.warning("AVService: PipeWire EQ apply failed: %s", exc)

    def _restore_pipewire_eq(self) -> None:
        """Re-elect DA EQ sink as default at startup without restarting filter-chain."""
        try:
            from src.audio import pipewire_eq
            pipewire_eq.ensure_default()
            if pipewire_eq.is_active() and self._audio is not None:
                self._audio.set_eq_preset("flat")
        except Exception as exc:
            log.warning("AVService: PipeWire EQ restore failed: %s", exc)

    # ── Worker bodies ──────────────────────────────────────────────────

    def _do_say(self, text: str, request_id: str | None = None) -> None:
        """Synchronous say — used as fallback when TTS has no render() method."""
        try:
            self.bus.publish("av.speaking_started", {"text": text, "ts": time.time()})
            self._tts.say(text, output=self._audio)
            payload = {"text": text, "ts": time.time()}
            if request_id is not None:
                payload["request_id"] = request_id
            self.bus.publish("av.spoke", payload)
        except Exception:
            log.exception("say(%r) failed", text)

    def _do_play_samples(
        self, samples, sr: int, text: str, request_id: str | None = None
    ) -> None:
        """Audio worker: play pre-synthesized samples (synthesis already done)."""
        try:
            self.bus.publish("av.speaking_started", {"text": text, "ts": time.time()})
            if self._audio is not None:
                self._audio.play(samples, sample_rate=sr)
            payload = {"text": text, "ts": time.time()}
            if request_id is not None:
                payload["request_id"] = request_id
            self.bus.publish("av.spoke", payload)
        except Exception:
            log.exception("play_samples(%r) failed", text)

    def _do_beep(self, freq: float, duration: float) -> None:
        try:
            self._audio.beep(frequency=freq, duration=duration)
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

    def _prewarm_tts(self) -> None:
        """Load the Piper ONNX model and pre-synthesize the startup phrase in the
        background. Submits to the synth executor (single-threaded) so synthesis
        is serialized with all subsequent av.say() calls.  The audio worker stays
        completely free during the entire ~21s prewarm+synthesis window."""
        self._synth_executor.submit(self._synth_startup_phrase)

    def _synth_startup_phrase(self) -> None:
        """Runs in synth executor: load model, pre-synthesize startup phrase,
        then enqueue playback-only to the audio worker."""
        try:
            t0 = time.monotonic()
            self._tts.prewarm()
            log.info("TTS model pre-warmed in %.1f s", time.monotonic() - t0)
        except Exception:
            log.exception("TTS pre-warm failed (non-fatal)")
            if self._announce_on_start:
                self._enqueue(self._do_announce_startup, label="announce_startup")
            return

        if not self._announce_on_start:
            return

        try:
            from src.core.version import get_version, spoken_version
            phrase = "VERA starting, " + spoken_version()
            t1 = time.monotonic()
            samples, sr = self._tts.render(phrase)
            log.info(
                "Startup phrase pre-synthesized in %.1f s (%.2fs audio)",
                time.monotonic() - t1,
                len(samples) / sr,
            )
            log.info("Speaking version: %s (%s)", get_version(), phrase)
            self._enqueue(
                lambda s=samples, r=sr, p=phrase: self._do_play_startup(s, r, p),
                label="announce_startup",
            )
        except Exception:
            log.exception("Startup phrase pre-synthesis failed — falling back")
            self._enqueue(self._do_announce_startup, label="announce_startup")

    def _do_announce_startup(self) -> None:
        try:
            self._announcer.announce_startup()
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("Startup version announcement failed")

    def _do_play_startup(self, samples, sr: int, phrase: str) -> None:
        """Audio worker: play pre-synthesized startup samples."""
        try:
            if self._audio is not None:
                self._audio.play(samples, sample_rate=sr)
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("Startup announcement playback failed")

    def _do_announce_request(self) -> None:
        try:
            self._announcer.announce_on_request()
            from src.core.version import get_version
            self.bus.publish("av.version_announced", {"version": get_version()})
        except Exception:
            log.exception("announce_version failed")

    def _collect_from_capture_svc(self, seconds: float) -> "np.ndarray":
        """Collect *seconds* of audio from the AudioCaptureService's running
        PortAudio stream. Avoids opening a second input stream that would
        conflict with the continuous capture loop (sd.rec race condition)."""
        import numpy as np

        svc = self._capture_svc
        mic = getattr(svc, "_mic", None)
        cfg = getattr(mic, "_cfg", None)
        rate = int(getattr(cfg, "sample_rate", 44100)) if cfg else 44100
        n_needed = int(seconds * rate)

        chunks: list["np.ndarray"] = []
        n_collected = 0
        last_index = svc.chunk_index()
        deadline = time.monotonic() + seconds + 10.0

        while n_collected < n_needed:
            if time.monotonic() > deadline:
                break
            if svc.chunk_index() == last_index:
                time.sleep(0.01)
                continue
            last_index = svc.chunk_index()
            chunk = svc.latest_chunk()
            if chunk is not None and chunk.size > 0:
                chunks.append(chunk)
                n_collected += chunk.size

        if not chunks:
            return np.zeros(n_needed, dtype=np.float32)
        combined = np.concatenate(chunks)
        return combined[:n_needed]

    def _do_record_clip(self, seconds: float, path: str | None = None) -> dict:
        secs = float(seconds)
        if secs <= 0:
            raise ValueError("seconds must be > 0")
        secs = min(secs, 120.0)

        rec_path = Path(path).expanduser() if path else self._default_recording_path()
        rec_path.parent.mkdir(parents=True, exist_ok=True)

        if self._capture_svc is not None and getattr(self._capture_svc, "hardware_ready", False):
            # Use the already-running capture stream to avoid PortAudio conflicts.
            import numpy as np
            data = self._collect_from_capture_svc(secs)
        else:
            if self._mic is None:
                from src.audio.input import AudioInput, AudioInputConfig
                self._mic = AudioInput(AudioInputConfig())
            if not bool(getattr(self._mic, "hardware_ready", False)):
                raise RuntimeError("microphone input unavailable (no active input device)")
            data = self._mic.record(secs)
        if data.ndim > 1:
            mono = data.mean(axis=1)
        else:
            mono = data
        mono = mono.astype("float32")
        import numpy as np

        # Determine sample rate from the source that was actually used.
        if self._capture_svc is not None and getattr(self._capture_svc, "hardware_ready", False):
            _cap_mic = getattr(self._capture_svc, "_mic", None)
            rate = int(getattr(getattr(_cap_mic, "_cfg", None), "sample_rate", 44100))
        else:
            rate = int(getattr(getattr(self._mic, "_cfg", None), "sample_rate", 44100))

        rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
        peak = float(np.max(np.abs(mono))) if mono.size else 0.0
        if peak < 0.001 and rms < 0.0003:
            raise RuntimeError(
                "recorded silence (no audible mic signal). Check input device/gain/wiring"
            )

        pcm = (mono.clip(-1.0, 1.0) * 32767.0).astype("int16")
        with wave.open(str(rec_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(rate)
            wf.writeframes(pcm.tobytes())

        self._last_recording_path = rec_path
        _hw_ready = (
            bool(getattr(self._capture_svc, "hardware_ready", False))
            if self._capture_svc is not None
            else bool(getattr(self._mic, "hardware_ready", False))
        )
        result = {
            "ok": True,
            "path": str(rec_path),
            "seconds": float(len(mono) / float(rate)),
            "sample_rate": rate,
            "samples": int(len(mono)),
            "hardware_ready": _hw_ready,
            "rms": rms,
            "peak": peak,
        }
        self.bus.publish("av.recorded", result)
        log.info("AVService: recorded %.2fs to %s", result["seconds"], rec_path)
        return result

    def _do_play_recording(self, path: str | None = None) -> dict:
        target = Path(path).expanduser() if path else self._last_recording_path
        if target is None:
            raise FileNotFoundError("no recording available")
        if not target.exists():
            raise FileNotFoundError(f"recording not found: {target}")
        if self._audio is None:
            raise RuntimeError("audio output unavailable")

        with wave.open(str(target), "rb") as wf:
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            rate = wf.getframerate()
            frames = wf.readframes(wf.getnframes())

        if sample_width != 2:
            raise ValueError("only 16-bit PCM WAV playback is supported")

        import numpy as np

        pcm = np.frombuffer(frames, dtype=np.int16)
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1).astype(np.int16)
        samples = (pcm.astype(np.float32) / 32767.0).clip(-1.0, 1.0)
        # For user-recorded clips, bypass TTS loudness/EQ processing so playback
        # is faithful and not over-colored or distorted.
        self._audio.play(
            samples,
            sample_rate=rate,
            blocking=True,
            apply_processing=False,
        )

        result = {
            "ok": True,
            "path": str(target),
            "sample_rate": int(rate),
            "samples": int(len(samples)),
            "seconds": float(len(samples) / float(rate)),
        }
        self.bus.publish("av.recording_played", result)
        log.info("AVService: played recording %s", target)
        return result

    def _default_recording_path(self) -> Path:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        _RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
        return _RECORDINGS_DIR / f"recording_{stamp}.wav"
