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
        """Play a numpy waveform. samples: 1-D mono or 2-D (n_samples, channels).

        If *sample_rate* differs from the device's configured rate, the
        waveform is linearly resampled — many USB DACs (incl. CM108) only
        accept 44.1/48 kHz, so we never hand them e.g. espeak's 22050.
        """
        sr = sample_rate or self._cfg.sample_rate
        if self._sim:
            log.debug("[sim] play() %d samples @ %d Hz", len(samples), sr)
            return
        target_sr = self._cfg.sample_rate
        if sr != target_sr:
            samples = _resample_linear(samples, sr, target_sr)
            sr = target_sr
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

    def chime(
        self,
        notes: Optional[tuple[float, ...]] = None,
        note_duration: float = 0.18,
        gap: float = 0.04,
        amplitude: float = 0.25,
    ) -> None:
        """Play an ascending arpeggio "chime" used as the boot signal.

        Each tone is a sine with a short attack/release envelope to
        avoid clicks. Duplicated to both channels so a user with only
        one speaker still hears it. Default notes are C5, E5, G5 — a
        cheerful major triad.
        """
        if notes is None:
            notes = (523.25, 659.25, 783.99)  # C5, E5, G5
        sr = self._cfg.sample_rate
        n_note = int(sr * note_duration)
        n_gap = int(sr * gap)
        # 5 ms attack/release envelope
        env_len = max(1, int(sr * 0.005))
        env = np.ones(n_note, dtype=np.float32)
        ramp = np.linspace(0.0, 1.0, env_len, dtype=np.float32)
        env[:env_len] = ramp
        env[-env_len:] = ramp[::-1]

        chunks = []
        t = np.linspace(0, note_duration, n_note, endpoint=False, dtype=np.float32)
        for i, freq in enumerate(notes):
            tone = amplitude * np.sin(2 * np.pi * freq * t).astype(np.float32) * env
            chunks.append(tone)
            if i < len(notes) - 1 and n_gap > 0:
                chunks.append(np.zeros(n_gap, dtype=np.float32))
        wave = np.concatenate(chunks)
        if self._cfg.channels == 2:
            wave = np.column_stack([wave, wave])
        self.play(wave, sample_rate=sr)


def _resample_linear(samples: np.ndarray, src_sr: int, dst_sr: int) -> np.ndarray:
    """Cheap linear resampler. Sufficient for speech and short tones; we
    don't want a scipy/librosa dep just for boot audio.
    """
    if src_sr == dst_sr or len(samples) == 0:
        return samples
    samples = np.asarray(samples)
    n_src = samples.shape[0]
    n_dst = int(round(n_src * dst_sr / src_sr))
    if n_dst <= 0:
        return samples[:0]
    src_x = np.linspace(0.0, 1.0, n_src, dtype=np.float64)
    dst_x = np.linspace(0.0, 1.0, n_dst, dtype=np.float64)
    if samples.ndim == 1:
        return np.interp(dst_x, src_x, samples).astype(np.float32)
    out = np.empty((n_dst, samples.shape[1]), dtype=np.float32)
    for c in range(samples.shape[1]):
        out[:, c] = np.interp(dst_x, src_x, samples[:, c])
    return out
