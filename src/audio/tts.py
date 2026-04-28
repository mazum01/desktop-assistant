"""
Text-to-speech for Desktop Assistant.

Backend: espeak-ng (apt: `espeak-ng`). Offline, lightweight, no network
calls. Higher-quality voices (Piper, Mimic 3) can be added later by
swapping the backend; the public `say()` API stays stable.

Two playback paths:
  * `say(text)`           — espeak-ng plays directly through ALSA default
  * `say(text, output)`   — render to WAV via espeak-ng, then play through
                             the project's AudioOutput (Sabrent USB)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class TTSConfig:
    voice: str = "en-us"          # espeak-ng voice / language
    speed_wpm: int = 165          # words per minute (130-200 sounds natural)
    pitch: int = 50               # 0-99
    amplitude: int = 200          # 0-200 (max — speakers run unamplified for now)


class TextToSpeech:
    """
    Wrapper around espeak-ng. Falls back to log-only sim mode if the
    binary is unavailable (so the codebase still imports on dev machines).
    """

    def __init__(self, config: Optional[TTSConfig] = None) -> None:
        self._cfg = config or TTSConfig()
        self._binary = shutil.which("espeak-ng") or shutil.which("espeak")
        self._sim = self._binary is None
        if self._sim:
            log.warning("[sim] espeak-ng not found — TTS in sim mode")

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def binary(self) -> Optional[str]:
        return self._binary

    # ── Public API ──────────────────────────────────────────────────────

    def say(self, text: str, output=None) -> None:
        """
        Speak *text* aloud.

        If *output* is None, espeak-ng plays directly through ALSA default.
        If *output* is an AudioOutput, the WAV is rendered then played
        through that device (so it goes through the Sabrent USB adapter).
        """
        if self._sim:
            log.info("[sim TTS] %s", text)
            return

        if output is None:
            self._speak_direct(text)
        else:
            samples, sr = self._render_to_array(text)
            output.play(samples, sample_rate=sr)

    def render_to_wav(self, text: str, path: str) -> None:
        """Render *text* to a WAV file at *path* (no playback)."""
        if self._sim:
            log.info("[sim TTS] would render to %s: %s", path, text)
            return
        self._run_espeak(text, wav_path=path)

    # ── Internal ────────────────────────────────────────────────────────

    def _speak_direct(self, text: str) -> None:
        self._run_espeak(text, wav_path=None)

    def _render_to_array(self, text: str) -> tuple[np.ndarray, int]:
        """Render to a temporary WAV, load as float32, return (samples, sr)."""
        import wave
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            self._run_espeak(text, wav_path=wav_path)
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                n = wf.getnframes()
                raw = wf.readframes(n)
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            # Peak-normalize to give the unamplified speaker every drop of
            # gain we can. -1 dBFS leaves a hair of headroom against rounding.
            peak = float(np.max(np.abs(samples))) if samples.size else 0.0
            if peak > 0.0:
                samples = samples * (0.89 / peak)
            return samples, sr
        finally:
            Path(wav_path).unlink(missing_ok=True)

    def _run_espeak(self, text: str, wav_path: Optional[str]) -> None:
        cmd = [
            self._binary,
            "-v", self._cfg.voice,
            "-s", str(self._cfg.speed_wpm),
            "-p", str(self._cfg.pitch),
            "-a", str(self._cfg.amplitude),
        ]
        if wav_path is not None:
            cmd += ["-w", wav_path]
        cmd.append(text)
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=30.0)
        except subprocess.CalledProcessError as exc:
            log.error("espeak-ng failed: %s", exc.stderr.decode(errors="ignore"))
        except subprocess.TimeoutExpired:
            log.error("espeak-ng timed out")
