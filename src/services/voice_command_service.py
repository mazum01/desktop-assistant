"""Voice command service: wake detection + streaming STT + intent routing."""

from __future__ import annotations

from collections import deque
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

import numpy as np

from src.core.service import Service
from src.voice.backends import (
    EnergyWakeWordDetector,
    EnergyWakeWordDetectorConfig,
    FasterWhisperSTT,
    FasterWhisperSTTConfig,
    NullStreamingSTT,
    ShellCommandSTT,
    ShellCommandSTTConfig,
    StreamingSTTBackend,
)
from src.voice.dialog_manager import DialogManager, DialogManagerConfig
from src.voice.intent_router import IntentRouter

log = logging.getLogger(__name__)

STATE_IDLE = "idle"
STATE_COMMAND_LISTEN = "command_listen"
STATE_THINKING = "thinking"
_VALID_STT_BACKENDS = {"faster_whisper", "shell", "null"}


@dataclass
class VoiceCommandConfig:
    enabled: bool = False
    poll_seconds: float = 0.08
    sample_rate: int = 16000
    wake_cooldown_s: float = 1.5
    wake_threshold_dbfs: float = -38.0
    wake_consecutive_frames: int = 2
    command_min_s: float = 0.35
    command_max_s: float = 6.0
    silence_end_s: float = 0.8
    stt_backend: str = "faster_whisper"
    stt_command: str = ""
    stt_language: str = "en"
    stt_timeout_s: float = 20.0
    stt_model: str = "base.en"
    stt_device: str = "cpu"
    stt_compute_type: str = "int8"
    dialog_timeout_s: float = 20.0


class VoiceCommandService(Service):
    """Consumes mic chunks and emits routed voice intents."""

    name = "voice_command"
    tick_seconds = 0.08

    def __init__(
        self,
        bus=None,
        capture_service=None,
        led=None,
        config: Optional[VoiceCommandConfig] = None,
        wake_detector=None,
        stt_backend: Optional[StreamingSTTBackend] = None,
        intent_router: Optional[IntentRouter] = None,
        dialog_manager: Optional[DialogManager] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._capture = capture_service
        self._led = led
        self._cfg = config or VoiceCommandConfig()
        self.tick_seconds = float(self._cfg.poll_seconds)

        self._wake = wake_detector or EnergyWakeWordDetector(
            EnergyWakeWordDetectorConfig(
                threshold_dbfs=float(self._cfg.wake_threshold_dbfs),
                consecutive_frames=int(self._cfg.wake_consecutive_frames),
            )
        )
        if stt_backend is not None:
            self._stt = stt_backend
        else:
            self._stt = self._build_stt_backend()
        self._router = intent_router or IntentRouter()
        self._dialog = dialog_manager or DialogManager(
            DialogManagerConfig(session_timeout_s=float(self._cfg.dialog_timeout_s))
        )

        self._state = STATE_IDLE
        self._tts_speaking = False
        self._vad_active = False
        self._last_wake_mono = 0.0
        self._cmd_started_mono = 0.0
        self._last_voice_mono = 0.0
        self._last_chunk_index = 0
        self._unsubs = []
        self._pre_roll_chunks: deque[np.ndarray] = deque()
        self._refresh_pre_roll_buffer()

    def _refresh_pre_roll_buffer(self) -> None:
        max_chunks = max(1, int(round(0.4 / max(0.02, float(self.tick_seconds)))))
        retained = list(self._pre_roll_chunks)[-max_chunks:]
        self._pre_roll_chunks = deque(retained, maxlen=max_chunks)

    def _build_stt_backend(self) -> StreamingSTTBackend:
        backend = str(self._cfg.stt_backend).lower()
        if backend == "faster_whisper":
            return FasterWhisperSTT(
                FasterWhisperSTTConfig(
                    sample_rate=int(self._cfg.sample_rate),
                    language=self._cfg.stt_language,
                    model=str(self._cfg.stt_model),
                    device=str(self._cfg.stt_device),
                    compute_type=str(self._cfg.stt_compute_type),
                )
            )
        if backend == "shell":
            return ShellCommandSTT(
                ShellCommandSTTConfig(
                    command=self._cfg.stt_command,
                    sample_rate=int(self._cfg.sample_rate),
                    language=self._cfg.stt_language,
                    timeout_s=float(self._cfg.stt_timeout_s),
                )
            )
        return NullStreamingSTT()

    def _apply_config_patch(self, patch: dict[str, Any]) -> None:
        if not patch:
            return
        prev_backend = str(self._cfg.stt_backend).lower()
        prev_rate = int(self._cfg.sample_rate)
        prev_cmd = str(self._cfg.stt_command)
        prev_lang = str(self._cfg.stt_language)
        prev_timeout = float(self._cfg.stt_timeout_s)
        prev_model = str(self._cfg.stt_model)
        prev_device = str(self._cfg.stt_device)
        prev_compute = str(self._cfg.stt_compute_type)

        for key, value in patch.items():
            if not hasattr(self._cfg, key):
                continue
            setattr(self._cfg, key, value)

        backend = str(self._cfg.stt_backend).lower()
        if backend not in _VALID_STT_BACKENDS:
            log.warning("VoiceCommandService: invalid stt_backend %r; keeping %r", backend, prev_backend)
            self._cfg.stt_backend = prev_backend
            backend = prev_backend
        else:
            self._cfg.stt_backend = backend

        if "poll_seconds" in patch:
            try:
                self.tick_seconds = max(0.02, float(self._cfg.poll_seconds))
            except (TypeError, ValueError):
                self.tick_seconds = max(0.02, self.tick_seconds)
            self._refresh_pre_roll_buffer()

        changed = (
            backend != prev_backend
            or int(self._cfg.sample_rate) != prev_rate
            or str(self._cfg.stt_command) != prev_cmd
            or str(self._cfg.stt_language) != prev_lang
            or float(self._cfg.stt_timeout_s) != prev_timeout
            or str(self._cfg.stt_model) != prev_model
            or str(self._cfg.stt_device) != prev_device
            or str(self._cfg.stt_compute_type) != prev_compute
        )
        if changed:
            try:
                self._stt.close()
            except (RuntimeError, OSError, ValueError):
                log.debug("voice stt backend close failed", exc_info=True)
            self._stt = self._build_stt_backend()

    def _on_set_config(self, _topic: str, payload: dict) -> None:
        if not isinstance(payload, dict):
            return
        self._apply_config_patch(payload)
        self.bus.publish("voice.config", {"config": self._cfg.__dict__.copy(), "ts": time.time()})

    def on_start(self) -> None:
        self._unsubs.append(self.bus.subscribe("audio.vad", self._on_vad))
        self._unsubs.append(self.bus.subscribe("av.speaking_started", self._on_speaking_started))
        self._unsubs.append(self.bus.subscribe("av.spoke", self._on_spoke))
        self._unsubs.append(self.bus.subscribe("voice.set_config", self._on_set_config))
        self._set_state(STATE_IDLE, reason="service_started")
        if self._capture is None:
            log.warning("VoiceCommandService: capture_service is unavailable")
        elif not self._cfg.enabled:
            log.info("VoiceCommandService: disabled by config")
        else:
            log.info("VoiceCommandService enabled (stt_backend=%s)", self._cfg.stt_backend)

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._stt.close()
        self._set_led_state("idle")

    def _on_vad(self, _topic: str, payload: dict) -> None:
        active = bool((payload or {}).get("active", False))
        self._vad_active = active
        if active:
            self._last_voice_mono = time.monotonic()

    def _on_speaking_started(self, _topic: str, _payload: dict) -> None:
        self._tts_speaking = True
        if self._state == STATE_COMMAND_LISTEN:
            self._set_state(STATE_IDLE, reason="tts_started")

    def _on_spoke(self, _topic: str, _payload: dict) -> None:
        self._tts_speaking = False

    def _set_led_state(self, state: str) -> None:
        led = self._led
        if led is None or not hasattr(led, "set_state"):
            return
        try:
            led.set_state(state)
        except (RuntimeError, OSError, ValueError):
            log.debug("voice led update failed", exc_info=True)

    def _set_state(self, state: str, *, reason: str) -> None:
        if self._state == state:
            return
        self._state = state
        if state == STATE_IDLE:
            self._set_led_state("idle")
        elif state == STATE_COMMAND_LISTEN:
            self._set_led_state("listening")
        elif state == STATE_THINKING:
            self._set_led_state("thinking")
        self.bus.publish(
            "voice.state",
            {"state": state, "reason": reason, "ts": time.time()},
        )

    def _read_latest_chunk(self) -> np.ndarray | None:
        cap = self._capture
        if cap is None or not hasattr(cap, "chunk_index") or not hasattr(cap, "latest_chunk"):
            return None
        idx = int(cap.chunk_index())
        if idx <= self._last_chunk_index:
            return None
        self._last_chunk_index = idx
        chunk = cap.latest_chunk()
        if chunk is None or getattr(chunk, "size", 0) == 0:
            return None
        return chunk.astype(np.float32, copy=False)

    def _start_command_window(self, now_mono: float) -> None:
        self._stt.start_stream()
        self._wake.reset()
        self._last_wake_mono = now_mono
        self._cmd_started_mono = now_mono
        self._last_voice_mono = now_mono
        for chunk in self._pre_roll_chunks:
            self._stt.accept_chunk(chunk, int(self._cfg.sample_rate))
        self._pre_roll_chunks.clear()
        self._set_state(STATE_COMMAND_LISTEN, reason="wake_detected")
        self.bus.publish(
            "voice.wake",
            {"ts": time.time(), "backend": "energy"},
        )

    def _dispatch_transcript(self, transcript: str, elapsed_s: float) -> None:
        self.bus.publish(
            "voice.transcript",
            {"text": transcript, "final": True, "ts": time.time(), "elapsed_s": elapsed_s},
        )
        decision = self._router.classify(transcript)
        self._dialog.observe(decision)
        resolved, payload = self._dialog.resolve_confirmation(decision)
        if resolved:
            if payload is None:
                self.bus.publish("av.say", {"text": "Okay, canceled."})
            else:
                self.bus.publish("voice.confirmed_action", payload)
            self.bus.publish(
                "voice.intent",
                {
                    "intent": decision.name,
                    "route": "dialog_resolution",
                    "confidence": decision.confidence,
                    "text": transcript,
                    "ts": time.time(),
                },
            )
            return

        if decision.route == "dialog_confirm":
            prompt = "Please confirm. " + transcript + ". Say yes to continue or no to cancel."
            self._dialog.set_pending_confirmation(decision.name, {"utterance": transcript})
            self.bus.publish("av.say", {"text": prompt})
        elif decision.route == "av_utterance":
            self.bus.publish("av.utterance", {"text": transcript, "source": "voice"})

        self.bus.publish(
            "voice.intent",
            {
                "intent": decision.name,
                "route": decision.route,
                "confidence": decision.confidence,
                "text": transcript,
                "dialog": self._dialog.snapshot(),
                "ts": time.time(),
            },
        )

    def _finish_command_window(self, now_mono: float) -> None:
        self._set_state(STATE_THINKING, reason="stt_finalize")
        transcript = self._stt.finalize().strip()
        elapsed = max(0.0, now_mono - self._cmd_started_mono)
        if transcript:
            self._dispatch_transcript(transcript, elapsed)
        else:
            self.bus.publish(
                "voice.intent",
                {
                    "intent": "empty",
                    "route": "none",
                    "confidence": 1.0,
                    "text": "",
                    "dialog": self._dialog.snapshot(),
                    "ts": time.time(),
                },
            )
        self._set_state(STATE_IDLE, reason="command_complete")

    def run_tick(self) -> None:
        if not self._cfg.enabled:
            return
        if self._tts_speaking:
            return
        chunk = self._read_latest_chunk()
        if chunk is None:
            return

        now = time.monotonic()
        if self._state == STATE_IDLE:
            self._pre_roll_chunks.append(chunk.astype(np.float32, copy=True))
            cooldown_elapsed = (now - self._last_wake_mono) >= float(self._cfg.wake_cooldown_s)
            if cooldown_elapsed and self._wake.process(chunk, int(self._cfg.sample_rate)):
                self._start_command_window(now)
            return

        if self._state != STATE_COMMAND_LISTEN:
            return

        self._stt.accept_chunk(chunk, int(self._cfg.sample_rate))
        if self._vad_active:
            self._last_voice_mono = now

        elapsed = now - self._cmd_started_mono
        if elapsed >= float(self._cfg.command_max_s):
            self._finish_command_window(now)
            return
        if elapsed < float(self._cfg.command_min_s):
            return
        if (now - self._last_voice_mono) >= float(self._cfg.silence_end_s):
            self._finish_command_window(now)
