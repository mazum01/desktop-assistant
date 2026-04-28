"""
Audio output — playback through the Sabrent USB audio adapter.

Uses sounddevice (PortAudio backend) for raw waveform playback. The
adapter is auto-located by name match against ALSA's device list;
override via AudioOutputConfig.device_name or device_index.

Falls back to simulation mode if no audio device is available.
"""

from __future__ import annotations

import logging
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
    log.warning("sounddevice not available — audio output in sim mode (%s)", exc)


@dataclass
class AudioOutputConfig:
    # Substrings matched against device names; any match wins. Covers
    # Sabrent (Realtek), C-Media (Unitek Y-247A and other generic CM108
    # adapters), and the kernel's generic "USB Audio Device" descriptor.
    device_names: tuple[str, ...] = ("USB Audio", "C-Media", "Sabrent")
    # Explicit override; if set, device_names is ignored.
    device_index: Optional[int] = None
    sample_rate: int = 48000  # 48 kHz is supported by every common USB DAC
    channels: int = 2

    # Back-compat: callers may still pass device_name="Foo".
    device_name: Optional[str] = None

    def __post_init__(self) -> None:
        # If a legacy single-string device_name was supplied, it REPLACES
        # the multi-name default (preserving old "match exactly this and
        # nothing else" semantics).
        if self.device_name:
            self.device_names = (self.device_name,)


def find_output_device(
    name_substring: str | tuple[str, ...] | list[str] = (
        "USB Audio", "C-Media", "Sabrent",
    ),
) -> Optional[int]:
    """Return the index of the first output device whose name contains
    any of *name_substring* (case-insensitive). None if nothing matches.
    Accepts a single string for back-compat."""
    if not _SD_AVAILABLE:
        return None
    try:
        devices = sd.query_devices()
    except Exception as exc:
        log.warning("query_devices failed: %s", exc)
        return None
    if isinstance(name_substring, str):
        needles = (name_substring.lower(),)
    else:
        needles = tuple(s.lower() for s in name_substring)
    for idx, dev in enumerate(devices):
        if dev.get("max_output_channels", 0) <= 0:
            continue
        name = dev.get("name", "").lower()
        if any(n in name for n in needles):
            return idx
    return None


class AudioOutput:
    """
    Thin wrapper over sounddevice for playing PCM audio.

    Usage:
        out = AudioOutput()
        out.play(numpy_array, sample_rate=22050)   # blocks until finished
        out.beep(frequency=440, duration=0.5)
    """

    def __init__(self, config: Optional[AudioOutputConfig] = None) -> None:
        self._cfg = config or AudioOutputConfig()
        self._sim = not _SD_AVAILABLE
        self._device_index: Optional[int] = None

        if self._sim:
            return

        if self._cfg.device_index is not None:
            self._device_index = self._cfg.device_index
        else:
            self._device_index = find_output_device(self._cfg.device_names)

        if self._device_index is None:
            log.warning(
                "[sim] No output device matched any of %s — sim mode",
                self._cfg.device_names,
            )
            self._sim = True

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def device_index(self) -> Optional[int]:
        return self._device_index

    @property
    def device_info(self) -> Optional[dict]:
        if self._sim or self._device_index is None:
            return None
        try:
            return sd.query_devices(self._device_index)
        except Exception:
            return None

    # ── Playback ────────────────────────────────────────────────────────

    def play(
        self,
        samples: np.ndarray,
        sample_rate: Optional[int] = None,
        blocking: bool = True,
    ) -> None:
        """Play a numpy waveform. samples: 1-D mono or 2-D (n_samples, channels)."""
        sr = sample_rate or self._cfg.sample_rate
        if self._sim:
            log.debug("[sim] play() %d samples @ %d Hz", len(samples), sr)
            return
        sd.play(samples, samplerate=sr, device=self._device_index)
        if blocking:
            sd.wait()

    def stop(self) -> None:
        if self._sim:
            return
        sd.stop()

    def beep(
        self,
        frequency: float = 440.0,
        duration: float = 0.5,
        amplitude: float = 0.2,
    ) -> None:
        """Play a sine-wave beep — handy for bring-up tests."""
        sr = self._cfg.sample_rate
        t = np.linspace(0, duration, int(sr * duration), endpoint=False)
        tone = (amplitude * np.sin(2 * np.pi * frequency * t)).astype(np.float32)
        if self._cfg.channels == 2:
            tone = np.column_stack([tone, tone])
        self.play(tone, sample_rate=sr)
