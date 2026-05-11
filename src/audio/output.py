"""
Audio output — playback through the Sabrent USB audio adapter.

Routes all audio through ``aplay -D pulse`` which hands off to PipeWire's
PulseAudio compatibility sink.  This keeps the raw ALSA hardware device free
for PipeWire's exclusive use, allowing pianobar and other PipeWire clients to
mix alongside TTS without competing for exclusive device access.

Falls back to simulation mode if ``aplay`` is not installed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

_APLAY_AVAILABLE: bool = shutil.which("aplay") is not None


@dataclass
class AudioOutputConfig:
    sample_rate: int = 44100
    channels: int = 2

    # Soft-clipping waveshaper drive — pumps perceived loudness on the
    # unamplified bring-up speaker. 1.0 = off (linear). 2.0-4.0 typical;
    # higher = louder + more harmonic distortion. 0 disables.
    loudness_boost: float = 2.0

    # ALSA device name passed to ``aplay -D``.  "pulse" routes through
    # PipeWire's PulseAudio compatibility layer without holding the raw
    # ALSA PCM device open in this process.
    alsa_device: str = "pulse"

    # EQ preset name (see AudioOutput.set_eq_preset / _build_sos).
    eq_preset: str = "flat"

    # User-defined EQ bands for the "custom" preset.
    # Each entry is (center_hz, gain_db, Q).  Q=1.0 is moderate bandwidth.
    # Example: [(200, 6.0, 1.0), (8000, 4.0, 1.0)]
    custom_eq_bands: list = None  # type: ignore  # populated in __post_init__

    # Back-compat fields kept so existing callers don't break.
    device_names: tuple = ("pulse", "USB Audio", "C-Media", "Sabrent")
    device_index: Optional[int] = None
    device_name: Optional[str] = None

    def __post_init__(self) -> None:
        if self.custom_eq_bands is None:
            self.custom_eq_bands = []


def find_output_device(
    name_substring=("pulse", "USB Audio", "C-Media", "Sabrent"),
) -> Optional[int]:
    """Legacy helper — kept for back-compat.  Always returns None because
    the aplay backend does not use sounddevice device indices."""
    return None


class AudioOutput:
    """
    PCM audio output routed through ``aplay -D pulse`` (PipeWire).

    A single ``aplay`` subprocess is kept alive across streaming chunks so
    that consecutive TTS sentences play without gaps or restarts.  It is
    closed (and the process reaped) after each complete utterance via
    ``flush()``.

    Usage:
        out = AudioOutput()
        out.play(numpy_array, sample_rate=22050)   # blocks until finished
        out.beep(frequency=440, duration=0.5)
    """

    def __init__(self, config: Optional[AudioOutputConfig] = None) -> None:
        self._cfg = config or AudioOutputConfig()
        self._sim = not _APLAY_AVAILABLE
        self._proc: Optional[subprocess.Popen] = None
        self._eq_cache: dict = {}

        if self._sim:
            log.warning("[sim] aplay not found — audio output in sim mode")

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def device_index(self) -> Optional[int]:
        """Always None — aplay backend does not use device indices."""
        return None

    @property
    def device_info(self) -> Optional[dict]:
        return None

    def set_eq_preset(self, preset: str) -> None:
        """Apply a named EQ preset to subsequent audio chunks."""
        self._cfg.eq_preset = preset
        # Invalidate cache for custom preset since bands may have changed
        for key in list(self._eq_cache):
            if key.startswith("custom@"):
                del self._eq_cache[key]
        log.info("EQ preset set to %r", preset)

    def set_custom_eq_bands(self, bands: list) -> None:
        """Set user-defined EQ bands and switch to 'custom' preset.

        *bands* is a list of dicts: [{"hz": float, "gain_db": float, "q": float}, ...]
        Each entry is a peaking EQ filter.  Missing q defaults to 1.0.
        """
        self._cfg.custom_eq_bands = [
            (float(b["hz"]), float(b["gain_db"]), float(b.get("q", 1.0)))
            for b in bands
        ]
        # Invalidate cache
        for key in list(self._eq_cache):
            if key.startswith("custom@"):
                del self._eq_cache[key]
        self._cfg.eq_preset = "custom"
        log.info("Custom EQ set: %d band(s)", len(self._cfg.custom_eq_bands))

    def _get_sos(self, preset: str, sample_rate: int):
        """Return cached SOS biquad coefficients for *preset* at *sample_rate*."""
        key = f"{preset}@{sample_rate}"
        if key not in self._eq_cache:
            if preset == "custom":
                self._eq_cache[key] = _build_custom_sos(
                    self._cfg.custom_eq_bands, sample_rate
                )
            else:
                self._eq_cache[key] = _build_sos(preset, sample_rate)
        return self._eq_cache[key]

    # ── aplay subprocess management ──────────────────────────────────────

    def _ensure_proc(self) -> subprocess.Popen:
        """Return the running aplay process, starting one if needed."""
        if self._proc is not None and self._proc.poll() is None:
            return self._proc
        cmd = [
            "aplay",
            "-D", self._cfg.alsa_device,
            "-f", "S16_LE",
            "-r", str(self._cfg.sample_rate),
            "-c", str(self._cfg.channels),
            "-",
        ]
        log.debug("Opening aplay process: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return self._proc

    def _samples_to_s16(
        self,
        samples: np.ndarray,
        sample_rate: Optional[int],
    ) -> bytes:
        """Resample, apply loudness boost, apply EQ, convert to stereo S16_LE bytes."""
        sr = sample_rate or self._cfg.sample_rate
        if sr != self._cfg.sample_rate:
            samples = _resample_linear(samples, sr, self._cfg.sample_rate)
        drive = self._cfg.loudness_boost
        if drive and drive > 1.0:
            samples = _soft_clip(samples, drive)
        # Apply EQ biquad filter if scipy is available and preset is not flat.
        preset = self._cfg.eq_preset
        if preset and preset != "flat":
            try:
                from scipy.signal import sosfilt  # type: ignore
                sos = self._get_sos(preset, self._cfg.sample_rate)
                if sos is not None:
                    if samples.ndim == 2:
                        samples = np.column_stack(
                            [sosfilt(sos, samples[:, ch]) for ch in range(samples.shape[1])]
                        )
                    else:
                        samples = sosfilt(sos, samples)
                    samples = np.clip(samples, -1.0, 1.0)
            except ImportError:
                pass
        if self._cfg.channels == 2 and samples.ndim == 1:
            samples = np.column_stack([samples, samples])
        return (np.clip(samples, -1.0, 1.0) * 32767).astype(np.int16).tobytes()

    # ── Streaming write / flush ──────────────────────────────────────────

    def write_chunk(
        self,
        samples: np.ndarray,
        sample_rate: Optional[int] = None,
    ) -> None:
        """Write a chunk of audio to the streaming aplay process.

        Does not block until playback finishes.  Call ``flush()`` after the
        last chunk to wait for the audio to fully play through.
        """
        if self._sim:
            return
        raw = self._samples_to_s16(samples, sample_rate)
        try:
            proc = self._ensure_proc()
            proc.stdin.write(raw)
            proc.stdin.flush()
        except BrokenPipeError:
            log.warning("aplay stdin closed unexpectedly; resetting")
            self._proc = None

    def flush(self) -> None:
        """Close aplay's stdin and wait for it to finish playing all audio."""
        if self._sim or self._proc is None:
            return
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        self._proc.wait()
        self._proc = None

    def close(self) -> None:
        """Terminate the aplay process (call when the service shuts down)."""
        if self._proc is not None:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            self._proc = None

    # ── Playback ────────────────────────────────────────────────────────

    def play(
        self,
        samples: np.ndarray,
        sample_rate: Optional[int] = None,
        blocking: bool = True,
    ) -> None:
        """Play a numpy waveform.

        samples: 1-D mono or 2-D (n_samples, channels).
        Resamples if sample_rate differs from the device rate.
        """
        if self._sim:
            sr = sample_rate or self._cfg.sample_rate
            log.debug("[sim] play() %d samples @ %d Hz", len(samples), sr)
            return
        self.write_chunk(samples, sample_rate=sample_rate)
        if blocking:
            self.flush()

    def stop(self) -> None:
        """Abort any ongoing playback immediately."""
        if self._sim:
            return
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

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
        amplitude: float = 0.9,
    ) -> None:
        """Play an ascending arpeggio "chime" used as the boot signal.

        Each tone is a sine with a short attack/release envelope to
        avoid clicks. Duplicated to both channels so a user with only
        one speaker still hears it. Default notes are A5, C#6, E6 — an
        A-major arpeggio sitting in the small-speaker resonance band
        (1-3 kHz) and the ear's most sensitive band, so it's
        substantially louder than a lower-pitched chime.
        """
        if notes is None:
            notes = (880.0, 1108.73, 1318.51)  # A5, C#6, E6
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


def _soft_clip(samples: np.ndarray, drive: float) -> np.ndarray:
    """tanh waveshaper. Pushes RMS up while keeping peaks <= 1.0.
    drive=1 is linear; 2-4 is the useful range for unamplified speech.
    Output is renormalized so peak == drive's natural ceiling, giving
    real loudness gain (not just shape change).
    """
    samples = np.asarray(samples, dtype=np.float32)
    if samples.size == 0 or drive <= 1.0:
        return samples
    peak = float(np.max(np.abs(samples)))
    if peak <= 0.0:
        return samples
    # Drive into tanh, then scale so peak ≈ 0.95 (leave a hair of headroom).
    shaped = np.tanh(drive * (samples / peak)).astype(np.float32)
    shaped *= 0.95 / float(np.tanh(drive))
    return shaped


def _lowshelf_sos(fc: float, gain_db: float, fs: float, S: float = 1.0):
    """Single-section low-shelf biquad (Audio EQ Cookbook).

    Returns a 1×6 numpy array [b0, b1, b2, 1.0, a1, a2] suitable for
    ``scipy.signal.sosfilt``.
    """
    import math
    A  = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * fc / fs
    cs = math.cos(w0)
    sn = math.sin(w0)
    alpha = sn / 2 * math.sqrt((A + 1 / A) * (1 / S - 1) + 2)

    b0 =     A * ((A + 1) - (A - 1) * cs + 2 * math.sqrt(A) * alpha)
    b1 =  2 * A * ((A - 1) - (A + 1) * cs)
    b2 =     A * ((A + 1) - (A - 1) * cs - 2 * math.sqrt(A) * alpha)
    a0 =          (A + 1) + (A - 1) * cs + 2 * math.sqrt(A) * alpha
    a1 =    -2 * ((A - 1) + (A + 1) * cs)
    a2 =          (A + 1) + (A - 1) * cs - 2 * math.sqrt(A) * alpha

    import numpy as _np
    return _np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _highshelf_sos(fc: float, gain_db: float, fs: float, S: float = 1.0):
    """Single-section high-shelf biquad (Audio EQ Cookbook).

    Returns a 1×6 numpy array [b0, b1, b2, 1.0, a1, a2].
    """
    import math
    A  = 10 ** (gain_db / 40.0)
    w0 = 2 * math.pi * fc / fs
    cs = math.cos(w0)
    sn = math.sin(w0)
    alpha = sn / 2 * math.sqrt((A + 1 / A) * (1 / S - 1) + 2)

    b0 =     A * ((A + 1) + (A - 1) * cs + 2 * math.sqrt(A) * alpha)
    b1 = -2 * A * ((A - 1) + (A + 1) * cs)
    b2 =     A * ((A + 1) + (A - 1) * cs - 2 * math.sqrt(A) * alpha)
    a0 =          (A + 1) - (A - 1) * cs + 2 * math.sqrt(A) * alpha
    a1 =     2 * ((A - 1) - (A + 1) * cs)
    a2 =          (A + 1) - (A - 1) * cs - 2 * math.sqrt(A) * alpha

    import numpy as _np
    return _np.array([[b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0]])


def _build_sos(preset: str, sample_rate: int):
    """Return scipy SOS biquad coefficients for the named preset, or None.

    Returns None if scipy is not installed or preset is unknown.
    Each preset is a list of second-order sections compatible with
    ``scipy.signal.sosfilt``.

    Note: EQ filtering is applied to TTS/voice audio only.  pianobar
    streams audio directly through its own PipeWire client and bypasses
    Python's audio pipeline entirely, so these presets do not affect music
    playback — only spoken text.
    """
    try:
        import numpy as _np
        from scipy.signal import iirpeak  # type: ignore
    except ImportError:
        return None

    fs = float(sample_rate)

    if preset == "bass_boost":
        return _lowshelf_sos(200.0, 6.0, fs)

    if preset == "treble_boost":
        return _highshelf_sos(8000.0, 4.0, fs)

    if preset == "vocal":
        from scipy.signal import tf2sos  # type: ignore
        b, a = iirpeak(2500.0, 1.5, fs=fs)
        return tf2sos(b, a)

    if preset == "loudness":
        lo = _lowshelf_sos(150.0, 6.0, fs)
        hi = _highshelf_sos(6000.0, 4.0, fs)
        return _np.vstack([lo, hi])

    if preset == "warm":
        lo = _lowshelf_sos(400.0, 3.0, fs)
        hi = _highshelf_sos(5000.0, -4.0, fs)
        return _np.vstack([lo, hi])

    return None


def _build_custom_sos(bands: list, sample_rate: int):
    """Build SOS coefficients from user-defined peaking EQ bands.

    *bands* is a list of (center_hz, gain_db, Q) tuples.
    Returns stacked SOS array or None if scipy is unavailable or bands is empty.
    """
    if not bands:
        return None
    try:
        import numpy as _np
        from scipy.signal import iirpeak, tf2sos  # type: ignore
    except ImportError:
        return None

    fs = float(sample_rate)
    sections = []
    for center_hz, gain_db, q in bands:
        if gain_db == 0.0:
            continue  # flat band — skip
        try:
            if gain_db > 0:
                b, a = iirpeak(float(center_hz), float(q), fs=fs)
                # Scale peak gain — iirpeak gives unity; apply gain manually
                gain_lin = 10 ** (gain_db / 20.0)
                b = b * gain_lin
            else:
                # Notch-like: apply negative gain via iirpeak then invert
                b, a = iirpeak(float(center_hz), float(q), fs=fs)
                gain_lin = 10 ** (gain_db / 20.0)
                b = b * gain_lin
            sections.append(tf2sos(b, a))
        except Exception:
            continue

    if not sections:
        return None
    return _np.vstack(sections)
