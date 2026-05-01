"""
Audio capture service.

Continuously samples the microphone in small chunks, computes signal
level (dBFS), and exposes the most recent chunk to in-process callers.
This is the foundation for VAD / wake-word / STT in Phase 3 — it just
keeps the mic running and hands fresh audio to whoever asks.

Topics published:
    audio.level   {"dbfs": float, "rms": float, "ts": float}
    audio.chunk   {"index": int, "samples": int, "rate": int}
    audio.error   {"reason": str}

Public accessor (in-process callers):
    svc.latest_chunk() → np.ndarray | None    (float32, mono)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class AudioCaptureService(Service):
    name = "audio_capture"
    tick_seconds = 0.0   # ticks set per-chunk via chunk_seconds

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        mic=None,
        chunk_seconds: float = 0.25,
    ) -> None:
        super().__init__(bus=bus)
        self._mic = mic
        self._chunk_seconds = float(chunk_seconds)
        self.tick_seconds = self._chunk_seconds   # chunk drives cadence
        self._latest: Optional[np.ndarray] = None
        self._index = 0
        self._lock = threading.Lock()

    def on_start(self) -> None:
        if self._mic is None:
            from src.audio.input import AudioInput, AudioInputConfig
            self._mic = AudioInput(AudioInputConfig())
        log.info(
            "AudioCaptureService started; hardware_ready=%s chunk=%.2fs",
            getattr(self._mic, "hardware_ready", False),
            self._chunk_seconds,
        )

    def run_tick(self) -> None:
        if self._mic is None:
            return
        # If we've already failed many times in a row, back off to avoid
        # hammering PortAudio (which can wedge the shared output stream
        # when the input stream keeps erroring). Bring-up: mic isn't
        # always wired yet.
        if getattr(self, "_consecutive_failures", 0) >= 3:
            return
        try:
            chunk = self._mic.record(self._chunk_seconds)
        except Exception:
            self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
            if self._consecutive_failures <= 3:
                log.exception("mic.record failed")
            if self._consecutive_failures == 3:
                log.warning(
                    "mic.record failed 3x; suppressing further attempts "
                    "until service restart (mic likely unplugged or "
                    "sample-rate mismatch)"
                )
            self.bus.publish("audio.error", {"reason": "record_failed"})
            return
        self._consecutive_failures = 0

        # Mono float32 expected. If multi-channel, mix to mono for level.
        if chunk.ndim > 1:
            mono = chunk.mean(axis=1)
        else:
            mono = chunk

        rms = float(np.sqrt(np.mean(mono.astype(np.float64) ** 2))) if mono.size else 0.0
        # dBFS: 0 dB = full scale (rms 1.0), silence ~ -inf clamped to -120.
        dbfs = 20.0 * math.log10(rms) if rms > 1e-6 else -120.0

        with self._lock:
            self._latest = mono
            self._index += 1
            idx = self._index

        ts = time.time()
        self.bus.publish("audio.level", {"dbfs": dbfs, "rms": rms, "ts": ts})
        self.bus.publish(
            "audio.chunk",
            {
                "index": idx,
                "samples": int(mono.size),
                "rate": getattr(self._mic, "_cfg", None) and self._mic._cfg.sample_rate,
            },
        )

    def on_stop(self) -> None:
        log.info("AudioCaptureService stopped")

    # ── Public accessors ───────────────────────────────────────────────

    def latest_chunk(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def chunk_index(self) -> int:
        with self._lock:
            return self._index
