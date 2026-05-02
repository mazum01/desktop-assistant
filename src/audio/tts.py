"""
Text-to-speech for Desktop Assistant.

Primary backend : Piper (neural, offline — piper-tts Python package).
Fallback backend: espeak-ng (robotic but zero-dependency).

Voice model search order (Piper):
  1. Path in TTSConfig.piper_model
  2. config/piper/<TTSConfig.piper_voice_name>.onnx  (relative to repo root)
  3. ~/.local/share/piper-voices/<TTSConfig.piper_voice_name>.onnx

Two playback paths:
  * ``say(text)``           — render to WAV, play through ALSA default
  * ``say(text, output)``   — render to array, play through AudioOutput
"""

from __future__ import annotations

import io  # noqa: F401 — kept for potential future wav-in-memory use
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice

log = logging.getLogger(__name__)

# Resolved at import time so tests can monkeypatch the package name.
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class TTSConfig:
    # ── Piper settings ──────────────────────────────────────────────────
    piper_voice_name: str = "en_US-lessac-high"
    piper_model: Optional[str] = None   # override with explicit .onnx path
    piper_length_scale: float = 1.15    # slightly slower — TNG-computer pacing
    piper_noise_scale: float = 0.3      # flatter prosody — measured computer delivery
    piper_noise_w: float = 0.5          # tighter phoneme timing

    # ── espeak-ng fallback settings ──────────────────────────────────────
    voice: str = "en-us"
    speed_wpm: int = 165
    pitch: int = 50
    amplitude: int = 200


class TextToSpeech:
    """
    Neural TTS via Piper, with espeak-ng as fallback.
    Falls back to log-only sim mode when neither backend is available.
    """

    def __init__(self, config: Optional[TTSConfig] = None) -> None:
        self._cfg = config or TTSConfig()
        self._voice = None          # loaded PiperVoice (lazy)
        self._piper_model_path: Optional[Path] = None
        self._espeak_binary: Optional[str] = None
        self._backend = self._detect_backend()

    # ── Backend detection ────────────────────────────────────────────────

    def _detect_backend(self) -> str:
        # 1. Try Piper
        model = self._resolve_piper_model()
        if model is not None:
            self._piper_model_path = model
            log.info("TTS backend: piper (%s)", model.name)
            return "piper"
        log.warning("Piper model not found — trying espeak-ng fallback")
        # 2. Try espeak-ng
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        if binary:
            self._espeak_binary = binary
            log.warning("TTS backend: espeak-ng (robotic — install piper model for better quality)")
            return "espeak"
        log.warning("[sim] No TTS backend found — running in sim mode")
        return "sim"

    def _resolve_piper_model(self) -> Optional[Path]:
        cfg = self._cfg
        # Explicit override
        if cfg.piper_model:
            p = Path(cfg.piper_model)
            return p if p.exists() else None
        name = cfg.piper_voice_name
        candidates = [
            _REPO_ROOT / "config" / "piper" / f"{name}.onnx",
            Path.home() / ".local" / "share" / "piper-voices" / f"{name}.onnx",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _load_voice(self):
        if self._voice is None:
            from piper.voice import PiperVoice
            self._voice = PiperVoice.load(
                str(self._piper_model_path),
                config_path=str(self._piper_model_path.with_suffix(".onnx.json")),
                use_cuda=False,
            )
        return self._voice

    # ── Properties ───────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return self._backend != "sim"

    @property
    def binary(self) -> Optional[str]:
        return self._espeak_binary

    # ── Public API ───────────────────────────────────────────────────────

    def say(self, text: str, output=None) -> None:
        """
        Speak *text*.

        If *output* is None, audio plays directly through ALSA default.
        If *output* is an AudioOutput, the rendered array is passed through
        it (so it goes via the Sabrent USB adapter + loudness pipeline).
        """
        if self._backend == "sim":
            log.info("[sim TTS] %s", text)
            return
        samples, sr = self._render_to_array(text)
        if output is None:
            sounddevice.play(samples, samplerate=sr)
            sounddevice.wait()
        else:
            output.play(samples, sample_rate=sr)

    def render_to_wav(self, text: str, path: str) -> None:
        """Render *text* to a WAV file at *path* (no playback)."""
        if self._backend == "sim":
            log.info("[sim TTS] would render to %s: %s", path, text)
            return
        import wave
        samples, sr = self._render_to_array(text)
        int16 = (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16)
        with wave.open(path, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(int16.tobytes())

    # ── Internal ─────────────────────────────────────────────────────────

    def _render_to_array(self, text: str) -> tuple[np.ndarray, int]:
        if self._backend == "piper":
            return self._render_piper(text)
        return self._render_espeak(text)

    def _render_piper(self, text: str) -> tuple[np.ndarray, int]:
        from piper.config import SynthesisConfig
        voice = self._load_voice()
        cfg = self._cfg
        syn_cfg = SynthesisConfig(
            length_scale=cfg.piper_length_scale,
            noise_scale=cfg.piper_noise_scale,
            noise_w_scale=cfg.piper_noise_w,
        )
        chunks = list(voice.synthesize(text, syn_config=syn_cfg))
        sr = chunks[0].sample_rate if chunks else voice.config.sample_rate
        if chunks:
            samples = np.concatenate([c.audio_float_array for c in chunks])
        else:
            samples = np.zeros(1, dtype=np.float32)
        return self._normalize(samples), sr

    def _render_espeak(self, text: str) -> tuple[np.ndarray, int]:
        import wave
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            wav_path = tmp.name
        try:
            cmd = [
                self._espeak_binary,
                "-v", self._cfg.voice,
                "-s", str(self._cfg.speed_wpm),
                "-p", str(self._cfg.pitch),
                "-a", str(self._cfg.amplitude),
                "-w", wav_path,
                text,
            ]
            subprocess.run(cmd, check=True, capture_output=True, timeout=30.0)
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            return self._normalize(samples), sr
        except (subprocess.CalledProcessError, EOFError, wave.Error) as exc:
            log.error("espeak-ng render failed: %s", exc)
            return np.zeros(1, dtype=np.float32), 22050
        except subprocess.TimeoutExpired:
            log.error("espeak-ng timed out")
            return np.zeros(1, dtype=np.float32), 22050
        finally:
            Path(wav_path).unlink(missing_ok=True)

    @staticmethod
    def _normalize(samples: np.ndarray) -> np.ndarray:
        """Peak-normalize to -1 dBFS (0.89 linear)."""
        peak = float(np.max(np.abs(samples))) if samples.size else 0.0
        if peak > 0.0:
            samples = samples * (0.89 / peak)
        return samples
