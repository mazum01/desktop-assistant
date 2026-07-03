"""Wake-word and streaming-STT backend primitives."""

from __future__ import annotations

import logging
import math
import re
import shlex
import subprocess
import tempfile
import threading
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

_PUNCT_ONLY_RE = re.compile(r"^[\W_]+$")  # no word characters → hallucinated punctuation


@dataclass
class EnergyWakeWordDetectorConfig:
    threshold_dbfs: float = -38.0
    consecutive_frames: int = 2


class EnergyWakeWordDetector:
    """Simple always-on wake detector based on sustained signal level.

    This is intentionally lightweight and dependency-free so the voice pipeline
    can run in development without external wake-word libraries.
    """

    def __init__(self, config: EnergyWakeWordDetectorConfig | None = None) -> None:
        self._cfg = config or EnergyWakeWordDetectorConfig()
        self._hits = 0

    def reset(self) -> None:
        self._hits = 0

    def process(self, samples: np.ndarray, sample_rate: int) -> bool:  # noqa: ARG002
        if samples.size == 0:
            self._hits = 0
            return False
        x = samples.astype(np.float64, copy=False)
        rms = float(np.sqrt(np.mean(x * x)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
        if dbfs >= self._cfg.threshold_dbfs:
            self._hits += 1
        else:
            self._hits = 0
        if self._hits >= max(1, int(self._cfg.consecutive_frames)):
            self._hits = 0
            return True
        return False


# ---------------------------------------------------------------------------
# openWakeWord-based detector
# ---------------------------------------------------------------------------

_OWW_CHUNK_SAMPLES = 1280  # openWakeWord requires exactly 1280 int16 samples per call
_OWW_SAMPLE_RATE = 16000   # only 16 kHz is supported


@dataclass
class OpenWakeWordDetectorConfig:
    model_name: str = "hey_jarvis_v0.1"
    threshold: float = 0.5
    refractory_s: float = 2.0  # suppress re-triggers for this many seconds after wake
    fallback_to_energy: bool = True
    energy_threshold_dbfs: float = -38.0
    energy_consecutive_frames: int = 2


class OpenWakeWordDetector:
    """Wake detector powered by openWakeWord (ONNX-based neural wake phrase).

    Accepts the same ``process(samples, sample_rate) -> bool`` / ``reset()``
    interface as :class:`EnergyWakeWordDetector`.

    Input chunks must be mono float32 at 16 kHz (the capture service default).
    Internally they are converted to int16 before passing to openWakeWord.

    Falls back to energy-based detection automatically if the ``openwakeword``
    package is not installed or the requested model file is missing.
    """

    def __init__(self, config: OpenWakeWordDetectorConfig | None = None) -> None:
        self._cfg = config or OpenWakeWordDetectorConfig()
        self._model = None
        self._model_lock = threading.Lock()
        self._model_failed = False
        self._last_trigger_mono: float = -999.0
        # Overflow buffer: carry forward leftover samples when input chunk < 1280
        self._overflow: np.ndarray = np.empty(0, dtype=np.int16)
        # Energy fallback (used when OWW unavailable)
        self._energy = EnergyWakeWordDetector(
            EnergyWakeWordDetectorConfig(
                threshold_dbfs=self._cfg.energy_threshold_dbfs,
                consecutive_frames=self._cfg.energy_consecutive_frames,
            )
        ) if self._cfg.fallback_to_energy else None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self) -> None:
        """Reset internal state (call after a wake is consumed)."""
        self._overflow = np.empty(0, dtype=np.int16)
        if self._energy:
            self._energy.reset()
        model = self._model
        if model is not None:
            try:
                model.reset()
            except Exception:  # noqa: BLE001
                pass

    def warm_up(self) -> None:
        """Pre-load the openWakeWord model on a background thread."""
        threading.Thread(
            target=self._ensure_model, daemon=True, name="oww-warmup"
        ).start()

    def process(self, samples: np.ndarray, sample_rate: int) -> bool:  # noqa: ARG002
        """Return True when a wake phrase is detected.

        *samples* should be mono float32 at 16 kHz; other sample rates are
        accepted but openWakeWord is only accurate at 16 kHz.
        """
        if samples.size == 0:
            return False

        model = self._ensure_model()
        if model is None:
            return self._energy.process(samples, sample_rate) if self._energy else False

        now = time.monotonic()
        if (now - self._last_trigger_mono) < self._cfg.refractory_s:
            return False

        # Convert float32 → int16
        int16 = np.clip(samples * 32768.0, -32768, 32767).astype(np.int16)

        # Prepend any leftover samples from the previous call
        if self._overflow.size:
            int16 = np.concatenate([self._overflow, int16])
            self._overflow = np.empty(0, dtype=np.int16)

        # Feed 1280-sample blocks; carry over any remainder
        triggered = False
        i = 0
        while i + _OWW_CHUNK_SAMPLES <= int16.size:
            block = int16[i : i + _OWW_CHUNK_SAMPLES]
            i += _OWW_CHUNK_SAMPLES
            try:
                preds = model.predict(block)
            except Exception as exc:  # noqa: BLE001
                log.debug("OpenWakeWordDetector.predict error: %s", exc)
                continue
            score = preds.get(self._cfg.model_name, 0.0)
            if score >= self._cfg.threshold:
                log.info(
                    "OpenWakeWordDetector: wake phrase detected (model=%s score=%.3f)",
                    self._cfg.model_name,
                    score,
                )
                self._last_trigger_mono = time.monotonic()
                model.reset()
                self._overflow = np.empty(0, dtype=np.int16)
                triggered = True
                break  # don't process further blocks — hand control to command window

        if i < int16.size:
            self._overflow = int16[i:]

        return triggered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_model(self):
        """Load the openWakeWord model once; thread-safe double-checked locking."""
        if self._model is not None:
            return self._model
        if self._model_failed:
            return None
        with self._model_lock:
            if self._model is not None:
                return self._model
            if self._model_failed:
                return None
            try:
                import openwakeword  # noqa: PLC0415
            except ImportError:
                log.warning(
                    "OpenWakeWordDetector: openwakeword not installed — "
                    "falling back to energy-based wake detection"
                )
                self._model_failed = True
                return None

            # Resolve model path inside the package's bundled resources
            pkg_dir = Path(openwakeword.__file__).parent
            model_path = pkg_dir / "resources" / "models" / f"{self._cfg.model_name}.onnx"
            if not model_path.exists():
                log.warning(
                    "OpenWakeWordDetector: model file not found: %s — "
                    "falling back to energy-based wake detection",
                    model_path,
                )
                self._model_failed = True
                return None

            started = time.monotonic()
            try:
                self._model = openwakeword.Model(
                    wakeword_model_paths=[str(model_path)],
                )
                log.info(
                    "OpenWakeWordDetector: loaded model=%s in %.2fs",
                    self._cfg.model_name,
                    time.monotonic() - started,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "OpenWakeWordDetector: failed to load model %r: %s — "
                    "falling back to energy-based wake detection",
                    self._cfg.model_name,
                    exc,
                )
                self._model_failed = True
                return None
            return self._model


class StreamingSTTBackend(ABC):
    """Interface for chunked speech-to-text backends."""

    @abstractmethod
    def start_stream(self) -> None:
        """Start a new utterance stream."""

    @abstractmethod
    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:
        """Accept a mono float32 chunk. Returns optional partial transcript."""

    @abstractmethod
    def finalize(self) -> str:
        """Finish and return final transcript."""

    def close(self) -> None:
        """Release resources if needed."""


class NullStreamingSTT(StreamingSTTBackend):
    """No-op STT backend used when no recognizer is configured."""

    def start_stream(self) -> None:
        return

    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:  # noqa: ARG002
        return None

    def finalize(self) -> str:
        return ""


@dataclass
class ShellCommandSTTConfig:
    command: str = ""
    sample_rate: int = 16000
    language: str = "en"
    timeout_s: float = 20.0


class ShellCommandSTT(StreamingSTTBackend):
    """Streaming STT backend that executes a local CLI command on finalize.

    The configured command must print the transcript to stdout and may include:
      - ``{wav_path}``
      - ``{sample_rate}``
      - ``{language}``
    """

    def __init__(self, config: ShellCommandSTTConfig | None = None) -> None:
        self._cfg = config or ShellCommandSTTConfig()
        self._chunks: list[np.ndarray] = []

    def start_stream(self) -> None:
        self._chunks.clear()

    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:  # noqa: ARG002
        if samples.size:
            self._chunks.append(samples.astype(np.float32, copy=True))
        return None

    def finalize(self) -> str:
        if not self._chunks:
            return ""
        cmd_tmpl = self._cfg.command.strip()
        if not cmd_tmpl:
            self._chunks.clear()
            return ""

        audio = np.concatenate(self._chunks, axis=0)
        self._chunks.clear()
        with tempfile.TemporaryDirectory(prefix="vera-stt-") as tmpdir:
            wav_path = Path(tmpdir) / "utterance.wav"
            pcm16 = np.clip(audio, -1.0, 1.0)
            pcm16 = (pcm16 * 32767.0).astype(np.int16)
            with wave.open(str(wav_path), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(int(self._cfg.sample_rate))
                wf.writeframes(pcm16.tobytes())

            cmd = cmd_tmpl.format(
                wav_path=str(wav_path),
                sample_rate=int(self._cfg.sample_rate),
                language=self._cfg.language,
            )
            args = shlex.split(cmd)
            if not args:
                return ""
            try:
                proc = subprocess.run(
                    args,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=float(self._cfg.timeout_s),
                )
            except subprocess.TimeoutExpired:
                log.warning("ShellCommandSTT timed out after %.1fs", float(self._cfg.timeout_s))
                return ""
            if proc.returncode != 0:
                return ""
            return (proc.stdout or "").strip()


@dataclass
class FasterWhisperSTTConfig:
    sample_rate: int = 16000
    language: str = "en"
    model: str = "base.en"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    cpu_threads: int = 2  # limit inference threads to avoid starving other services


class FasterWhisperSTT(StreamingSTTBackend):
    """Persistent Faster-Whisper backend that keeps the model loaded in-process."""

    _MIN_SPEECH_DBFS: float = -50.0  # skip transcription if RMS is below this

    def __init__(self, config: FasterWhisperSTTConfig | None = None) -> None:
        self._cfg = config or FasterWhisperSTTConfig()
        self._chunks: list[np.ndarray] = []
        self._model = None
        self._model_lock = threading.Lock()   # guards one-time model loading
        self._infer_lock = threading.Lock()   # prevents concurrent inference calls

    def start_stream(self) -> None:
        self._chunks.clear()

    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:  # noqa: ARG002
        if samples.size:
            self._chunks.append(samples.astype(np.float32, copy=True))
        return None

    def _ensure_model(self):
        """Load the model exactly once; safe to call from multiple threads."""
        if self._model is not None:  # fast path — no lock needed
            return self._model
        with self._model_lock:
            if self._model is not None:  # re-check under lock
                return self._model
            started = time.monotonic()
            try:
                from faster_whisper import WhisperModel
            except ImportError:
                log.warning("FasterWhisperSTT unavailable: faster_whisper is not installed")
                return None
            try:
                self._model = WhisperModel(
                    self._cfg.model,
                    device=self._cfg.device,
                    compute_type=self._cfg.compute_type,
                    local_files_only=True,
                    cpu_threads=int(self._cfg.cpu_threads),
                )
            except Exception:  # noqa: BLE001 — fall back to allowing download
                try:
                    self._model = WhisperModel(
                        self._cfg.model,
                        device=self._cfg.device,
                        compute_type=self._cfg.compute_type,
                        cpu_threads=int(self._cfg.cpu_threads),
                    )
                except (RuntimeError, ValueError, OSError) as exc:
                    log.warning("FasterWhisperSTT failed to load model %r: %s", self._cfg.model, exc)
                    return None
            log.info(
                "FasterWhisperSTT loaded model=%s device=%s compute=%s in %.2fs",
                self._cfg.model,
                self._cfg.device,
                self._cfg.compute_type,
                time.monotonic() - started,
            )
            return self._model

    def warm_up(self) -> None:
        """Pre-load the model on a background thread so the first command isn't penalized."""
        threading.Thread(
            target=self._ensure_model, daemon=True, name="fw-stt-warmup"
        ).start()

    def finalize(self) -> str:
        if not self._chunks:
            return ""
        model = self._ensure_model()
        if model is None:
            self._chunks.clear()
            return ""

        audio = np.concatenate(self._chunks, axis=0)
        self._chunks.clear()

        rms = float(np.sqrt(np.mean(audio * audio)))
        dbfs = 20.0 * math.log10(rms) if rms > 1e-9 else -120.0
        if dbfs < self._MIN_SPEECH_DBFS:
            log.debug("FasterWhisperSTT: audio too quiet (%.1f dBFS), skipping", dbfs)
            return ""

        try:
            with self._infer_lock:
                segments, _info = model.transcribe(
                    audio,
                    language=self._cfg.language,
                    beam_size=int(self._cfg.beam_size),
                    condition_on_previous_text=False,
                )
                result = " ".join(seg.text.strip() for seg in segments).strip()
        except (RuntimeError, ValueError, OSError) as exc:
            log.warning("FasterWhisperSTT transcription failed: %s", exc)
            return ""
        log.info("FasterWhisperSTT: %.1f dBFS -> %r", dbfs, result)
        if _PUNCT_ONLY_RE.match(result):
            log.debug("FasterWhisperSTT: discarding punctuation-only hallucination %r", result)
            return ""
        return result
