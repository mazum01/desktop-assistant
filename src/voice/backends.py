"""Wake-word and streaming-STT backend primitives."""

from __future__ import annotations

import logging
import math
import shlex
import subprocess
import tempfile
import time
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)


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
    beam_size: int = 5


class FasterWhisperSTT(StreamingSTTBackend):
    """Persistent Faster-Whisper backend that keeps the model loaded in-process."""

    def __init__(self, config: FasterWhisperSTTConfig | None = None) -> None:
        self._cfg = config or FasterWhisperSTTConfig()
        self._chunks: list[np.ndarray] = []
        self._model = None

    def start_stream(self) -> None:
        self._chunks.clear()

    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:  # noqa: ARG002
        if samples.size:
            self._chunks.append(samples.astype(np.float32, copy=True))
        return None

    def _ensure_model(self):
        if self._model is not None:
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

    def finalize(self) -> str:
        if not self._chunks:
            return ""
        model = self._ensure_model()
        if model is None:
            self._chunks.clear()
            return ""

        audio = np.concatenate(self._chunks, axis=0)
        self._chunks.clear()
        try:
            segments, _info = model.transcribe(
                audio,
                language=self._cfg.language,
                beam_size=int(self._cfg.beam_size),
                condition_on_previous_text=False,
                vad_filter=True,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            log.warning("FasterWhisperSTT transcription failed: %s", exc)
            return ""
        return " ".join(seg.text.strip() for seg in segments).strip()
