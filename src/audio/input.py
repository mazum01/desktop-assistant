"""
Audio input — microphone capture.

Captures from the configured input device (defaults to the system
default mic; override via name substring or device index). Provides
both blocking `record()` and a streaming context manager for VAD/STT.

Falls back to simulation mode if no audio input device is available.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except (ImportError, OSError) as exc:
    sd = None  # type: ignore
    _SD_AVAILABLE = False
    log.warning("sounddevice not available — audio input in sim mode (%s)", exc)


@dataclass
class AudioInputConfig:
    # If device_index is set, it wins. Otherwise device_name is matched.
    # Empty device_name → use system default input.
    device_name: str = ""
    device_index: Optional[int] = None
    sample_rate: int = 16000  # 16 kHz is standard for STT / wake-word
    channels: int = 1


def find_input_device(name_substring: str = "") -> Optional[int]:
    """Return the index of the first input device matching *name_substring*
    (case-insensitive). Empty substring → None (use system default)."""
    if not _SD_AVAILABLE or not name_substring:
        return None
    try:
        devices = sd.query_devices()
    except Exception as exc:
        log.warning("query_devices failed: %s", exc)
        return None
    needle = name_substring.lower()
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) <= 0:
            continue
        if needle in dev.get("name", "").lower():
            return idx
    return None


class AudioInput:
    """
    Microphone capture wrapper.

    Usage:
        mic = AudioInput()
        samples = mic.record(seconds=3)   # blocks, returns float32 ndarray
    """

    def __init__(self, config: Optional[AudioInputConfig] = None) -> None:
        self._cfg = config or AudioInputConfig()
        self._sim = not _SD_AVAILABLE
        self._device_index: Optional[int] = None

        if self._sim:
            return

        if self._cfg.device_index is not None:
            self._device_index = self._cfg.device_index
        elif self._cfg.device_name:
            self._device_index = find_input_device(self._cfg.device_name)
            if self._device_index is None:
                log.warning(
                    "[sim] No input device matched '%s' — sim mode",
                    self._cfg.device_name,
                )
                self._sim = True
        # else: leave None → sounddevice uses system default

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def device_index(self) -> Optional[int]:
        return self._device_index

    @property
    def device_info(self) -> Optional[dict]:
        if self._sim:
            return None
        try:
            return sd.query_devices(
                self._device_index if self._device_index is not None else None,
                kind="input",
            )
        except Exception:
            return None

    # ── Capture ─────────────────────────────────────────────────────────

    def record(self, seconds: float) -> np.ndarray:
        """Block and return *seconds* of audio as a float32 numpy array.
        Shape: (n_samples,) for mono, (n_samples, channels) for multichannel.
        Sim mode returns silence."""
        n_samples = int(seconds * self._cfg.sample_rate)
        if self._sim:
            shape = (n_samples,) if self._cfg.channels == 1 else (n_samples, self._cfg.channels)
            return np.zeros(shape, dtype=np.float32)
        data = sd.rec(
            n_samples,
            samplerate=self._cfg.sample_rate,
            channels=self._cfg.channels,
            device=self._device_index,
            dtype="float32",
        )
        sd.wait()
        if self._cfg.channels == 1:
            return data[:, 0]
        return data
