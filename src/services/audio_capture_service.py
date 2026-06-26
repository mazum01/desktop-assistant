"""
Audio capture service.

Continuously samples the microphone in small chunks, computes signal level
(dBFS), voice-activity state, and a compact FFT spectrum for the web analyzer,
and exposes the most recent chunk to in-process callers.

Topics published:
    audio.level          {"dbfs": float, "rms": float, "ts": float, "speaking": bool}
    audio.chunk          {"index": int, "samples": int, "rate": int}
    audio.spectrum       {"bins": [float], "sample_rate": int, "max_hz": float, "ts": float}
    audio.vad            {"active": bool, "dbfs": float, "threshold_dbfs": float,
                           "state_changed": bool, "ts": float}
    audio.capture_stats  {"chunk_index": int, "consecutive_failures": int,
                           "hardware_ready": bool, "ts": float}
    audio.error          {"reason": str}

Public accessor (in-process callers):
    svc.latest_chunk() → np.ndarray | None    (float32, mono)
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


@dataclass
class AudioCaptureConfig:
    chunk_seconds: float = 0.25
    spectrum_bins: int = 48
    emit_spectrum: bool = True
    vad_threshold_dbfs: float = -42.0
    vad_hang_s: float = 0.8


class AudioCaptureService(Service):
    name = "audio_capture"
    tick_seconds = 0.0   # ticks set per-chunk via chunk_seconds

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        mic=None,
        chunk_seconds: float = 0.25,
        config: Optional[AudioCaptureConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._mic = mic
        self._cfg = config or AudioCaptureConfig(chunk_seconds=float(chunk_seconds))
        self._chunk_seconds = float(self._cfg.chunk_seconds)
        self.tick_seconds = self._chunk_seconds
        self._latest: Optional[np.ndarray] = None
        self._index = 0
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._speaking = False
        self._speaking_until = 0.0
        self._last_stats_ts = 0.0
        self._spectrum_prev: Optional[list[float]] = None

    def on_start(self) -> None:
        if self._mic is None:
            from src.audio.input import AudioInput, AudioInputConfig
            self._mic = AudioInput(AudioInputConfig())
        log.info(
            "AudioCaptureService started; hardware_ready=%s chunk=%.2fs vad=%.1fdBfs hang=%.2fs",
            getattr(self._mic, "hardware_ready", False),
            self._chunk_seconds,
            self._cfg.vad_threshold_dbfs,
            self._cfg.vad_hang_s,
        )

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._mic, "hardware_ready", False))

    def _sample_rate(self) -> int:
        cfg = getattr(self._mic, "_cfg", None)
        rate = getattr(cfg, "sample_rate", None)
        try:
            return int(rate) if rate else 16000
        except Exception:
            return 16000

    def _compute_spectrum(self, mono: np.ndarray) -> Optional[dict]:
        if not self._cfg.emit_spectrum or mono.size < 32:
            return None

        x = mono.astype(np.float64)
        if not np.any(x):
            bins = [0.0] * max(8, int(self._cfg.spectrum_bins))
            return {
                "bins": bins,
                "sample_rate": self._sample_rate(),
                "max_hz": float(self._sample_rate() / 2.0),
                "ts": time.time(),
            }

        # Window + rFFT for stable visual bars.
        window = np.hanning(x.size)
        window_sum = float(np.sum(window))
        if window_sum <= 0.0:
            return None

        spec = np.fft.rfft(x * window)
        # Amplitude-normalized FFT so dB maps to true full-scale behavior.
        mag = np.abs(spec) * (2.0 / window_sum)
        if mag.size <= 2:
            return None

        # Drop DC so low-bin flicker doesn't dominate the graph.
        mag = mag[1:]
        n_bins = max(8, int(self._cfg.spectrum_bins))
        n_bins = min(n_bins, mag.size)
        edges = np.linspace(0, mag.size, n_bins + 1, dtype=int)

        # Keep quiet rooms visually quiet and scale with measured signal level.
        rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
        room_dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
        floor_db = -85.0 if room_dbfs < -55.0 else -75.0
        denom = max(1e-9, -floor_db)

        out = []
        for i in range(n_bins):
            seg = mag[edges[i]:edges[i + 1]]
            level = float(np.sqrt(np.mean(seg * seg))) if seg.size else 0.0
            db = 20.0 * math.log10(level + 1e-12)
            db = max(floor_db, min(0.0, db))
            out.append((db - floor_db) / denom)

        # For mic-only capture, keep low-level content readable in noisy rooms
        # while preserving near-silence behavior.
        if room_dbfs > -65.0:
            out = [min(1.0, v * 1.8) for v in out]
        elif room_dbfs < -80.0:
            out = [max(0.0, v - 0.02) for v in out]

        # Smooth frame-to-frame to reduce flicker from ambient mic noise.
        prev = self._spectrum_prev
        if prev is not None and len(prev) == len(out):
            out = [0.3 * pv + 0.7 * ov for pv, ov in zip(prev, out)]
        self._spectrum_prev = out

        return {
            "bins": out,
            "sample_rate": self._sample_rate(),
            "max_hz": float(self._sample_rate() / 2.0),
            "ts": time.time(),
        }

    def _compute_vad(self, dbfs: float, now_mono: float) -> tuple[bool, bool]:
        if dbfs >= self._cfg.vad_threshold_dbfs:
            self._speaking_until = now_mono + self._cfg.vad_hang_s
        active = now_mono < self._speaking_until
        changed = active != self._speaking
        self._speaking = active
        return active, changed

    def _publish_stats(self, ts: float) -> None:
        if ts - self._last_stats_ts < 1.0:
            return
        self._last_stats_ts = ts
        self.bus.publish(
            "audio.capture_stats",
            {
                "chunk_index": self._index,
                "consecutive_failures": self._consecutive_failures,
                "hardware_ready": self.hardware_ready,
                "ts": ts,
            },
        )

    def run_tick(self) -> None:
        if self._mic is None:
            return
        # If we've already failed many times in a row, back off to avoid
        # hammering the input backend when the mic path is unavailable.
        if self._consecutive_failures >= 3:
            self._publish_stats(time.time())
            return
        try:
            chunk = self._mic.record(self._chunk_seconds)
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures <= 3:
                log.exception("mic.record failed")
            if self._consecutive_failures == 3:
                log.warning(
                    "mic.record failed 3x; suppressing further attempts "
                    "until service restart (mic likely unplugged or "
                    "sample-rate mismatch)"
                )
            self.bus.publish("audio.error", {"reason": "record_failed"})
            self._publish_stats(time.time())
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

        now_wall = time.time()
        now_mono = time.monotonic()
        speaking, changed = self._compute_vad(dbfs, now_mono)

        self.bus.publish("audio.level", {"dbfs": dbfs, "rms": rms, "ts": now_wall, "speaking": speaking})
        self.bus.publish(
            "audio.chunk",
            {
                "index": idx,
                "samples": int(mono.size),
                "rate": self._sample_rate(),
            },
        )

        spectrum = self._compute_spectrum(mono)
        if spectrum is not None:
            self.bus.publish("audio.spectrum", spectrum)

        self.bus.publish(
            "audio.vad",
            {
                "active": speaking,
                "dbfs": dbfs,
                "threshold_dbfs": float(self._cfg.vad_threshold_dbfs),
                "state_changed": changed,
                "ts": now_wall,
            },
        )
        self._publish_stats(now_wall)

    def on_stop(self) -> None:
        # Release the mic backend (e.g. terminate the pw-record subprocess)
        # so shutdown is clean and bounded.
        mic = self._mic
        if mic is not None and hasattr(mic, "close"):
            try:
                mic.close()
            except Exception:
                log.debug("AudioCaptureService: mic.close() failed", exc_info=True)
        log.info("AudioCaptureService stopped")

    # ── Public accessors ───────────────────────────────────────────────

    def latest_chunk(self) -> Optional[np.ndarray]:
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def chunk_index(self) -> int:
        with self._lock:
            return self._index
