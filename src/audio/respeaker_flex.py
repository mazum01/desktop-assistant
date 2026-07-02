"""
ReSpeaker Flex Linear audio backend.

Provides input/output classes that match the API of the default
:class:`~src.audio.input.AudioInput` and
:class:`~src.audio.output.AudioOutput` so they can be swapped in via the
audio factory without changing any downstream code.

Hardware
--------
The ReSpeaker Flex Linear is a USB linear mic array from Seeed Studio.
On Linux it registers as a standard USB Audio Class device (no custom
driver required).  Key characteristics:

* **ALSA card name** — typically ``"ReSpeaker"``; confirm with
  ``arecord -l`` after plugging in.
* **Input channels** — 6 by default (4 mic channels + 2 playback reference);
  channel 0 carries the AEC/beamformed processed output.
* **Native capture rate** — 16 000 Hz.
* **Output** — built-in speaker exposed as the same ALSA card's playback
  device.  Defaults to routing through PulseAudio/PipeWire (``-D pulse``)
  for mixing; set ``output_alsa_device: "plughw:ReSpeaker,0"`` in config
  to drive the speaker directly.

LED ring (optional)
-------------------
Install ``usb-pixel-ring-v2`` (``pip install usb-pixel-ring-v2``) to
enable visual status feedback.  If the package is absent,
:class:`ReSpeakerFlexLED` runs as a silent no-op.  LED state changes are
also published on the ``respeaker.led`` bus topic for observability even
when the hardware library is unavailable.

Selecting this backend
----------------------
In ``config/assistant.yaml``::

    audio:
      backend: respeaker_flex
      respeaker_flex:
        input_device_name: ReSpeaker
        input_sample_rate: 16000
        input_raw_channels: 6
        input_processed_channel: 0
        output_alsa_device: pulse
        output_sample_rate: 44100
        loudness_boost: 2.0
        eq_preset: flat
        led_enabled: true
"""

from __future__ import annotations

import logging
import queue
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Optional official ReSpeaker mic-array stack (PyAudio) ─────────────────────
try:
    import pyaudio  # type: ignore
    _PYAUDIO_AVAILABLE = True
except (ImportError, OSError) as _exc:
    pyaudio = None  # type: ignore
    _PYAUDIO_AVAILABLE = False
    log.warning("pyaudio not available — ReSpeaker official input disabled (%s)", _exc)

# ── Optional sounddevice fallback (legacy path) ───────────────────────────────
try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except (ImportError, OSError) as _exc:
    sd = None  # type: ignore
    _SD_AVAILABLE = False
    log.warning("sounddevice not available — ReSpeaker legacy fallback disabled (%s)", _exc)

# ── Optional LED library ──────────────────────────────────────────────────────
try:
    from pixel_ring import pixel_ring as _pixel_ring  # type: ignore
    _LED_LIB = "pixel_ring"
    _LED_AVAILABLE = True
except (ImportError, Exception):
    _pixel_ring = None
    _LED_LIB = None
    _LED_AVAILABLE = False

if not _LED_AVAILABLE:
    try:
        import usb.core as _usb_core  # type: ignore
        # usb_pixel_ring_v2 uses pyusb; attempt to import its ring object
        from usb_pixel_ring_v2 import PixelRing as _UsbPixelRing  # type: ignore
        _LED_LIB = "usb_pixel_ring_v2"
        _LED_AVAILABLE = True
    except (ImportError, Exception):
        _UsbPixelRing = None

# LED state constants — published on the respeaker.led bus topic
LED_STATE_IDLE      = "idle"
LED_STATE_LISTENING = "listening"
LED_STATE_SPEAKING  = "speaking"
LED_STATE_THINKING  = "thinking"
LED_STATE_ERROR     = "error"


# ─────────────────────────────────────────────────────────────────────────────
# Input
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReSpeakerFlexInputConfig:
    # ALSA device name substring — matched case-insensitively against
    # sounddevice's device list.  Use ``arecord -l`` to find the exact name.
    device_name: str = "ReSpeaker"
    # Override device_name by specifying a sounddevice device index directly.
    device_index: Optional[int] = None
    # Native sample rate of the ReSpeaker Flex Linear.
    sample_rate: int = 16000
    # Total channels captured from USB (4 mic + 2 reference typical).
    raw_channels: int = 6
    # Which channel index contains the processed (AEC/beamformed) output.
    # Channel 0 is the beamformed output on most ReSpeaker products.
    processed_channel: int = 0


class ReSpeakerFlexInput:
    """Microphone capture for the ReSpeaker Flex Linear USB array.

    Captures *raw_channels* from the USB device and extracts
    *processed_channel* as a mono float32 array — identical output format
    to :class:`~src.audio.input.AudioInput`.

    Usage::

        mic = ReSpeakerFlexInput()
        samples = mic.record(seconds=3)   # float32, shape (n,)
    """

    def __init__(self, config: Optional[ReSpeakerFlexInputConfig] = None) -> None:
        self._cfg = config or ReSpeakerFlexInputConfig()
        self._sim = False
        self._device_index: Optional[int] = None
        self._mode = "sim"
        self._pa = None
        self._stream = None
        self._queue: "queue.Queue[bytes]" = queue.Queue(maxsize=200)

        # Prefer the official ReSpeaker mic_array capture model (PyAudio callback
        # + queue), then fall back to our older sounddevice implementation if the
        # official stack is unavailable.
        if _PYAUDIO_AVAILABLE:
            if self._init_pyaudio():
                self._mode = "official"
                self._sim = False
                return
        if _SD_AVAILABLE and self._init_sounddevice():
            self._mode = "legacy"
            self._sim = False
            return

        self._sim = True
        log.warning(
            "[sim] ReSpeaker input unavailable — neither official (pyaudio) "
            "nor legacy (sounddevice) backend could initialize"
        )

    def _find_pyaudio_device(self, pa) -> Optional[int]:
        needle = self._cfg.device_name.lower()
        exact_channel_match: Optional[int] = None
        for idx in range(pa.get_device_count()):
            try:
                dev = pa.get_device_info_by_index(idx)
            except Exception:
                continue
            name = str(dev.get("name", ""))
            ch = int(dev.get("maxInputChannels", 0) or 0)
            if ch <= 0:
                continue
            if needle in name.lower():
                return idx
            # Official ReSpeaker examples select by channel count when names are
            # not predictable across hosts.
            if exact_channel_match is None and ch == int(self._cfg.raw_channels):
                exact_channel_match = idx
        if exact_channel_match is not None:
            return exact_channel_match
        for idx in range(pa.get_device_count()):
            try:
                dev = pa.get_device_info_by_index(idx)
            except Exception:
                continue
            if int(dev.get("maxInputChannels", 0) or 0) > 0:
                return idx
        return None

    def _init_pyaudio(self) -> bool:
        try:
            pa = pyaudio.PyAudio()
        except Exception as exc:
            log.warning("ReSpeaker official input init failed (PyAudio create): %s", exc)
            return False

        if self._cfg.device_index is not None:
            self._device_index = self._cfg.device_index
        else:
            self._device_index = self._find_pyaudio_device(pa)

        if self._device_index is None:
            try:
                pa.terminate()
            except Exception:
                pass
            log.warning(
                "[sim] ReSpeaker official pipeline: no input device matched %r",
                self._cfg.device_name,
            )
            return False

        frames_per_buffer = max(1, int(self._cfg.sample_rate * 0.08))

        def _callback(in_data, frame_count, time_info, status):  # noqa: ARG001
            try:
                self._queue.put_nowait(in_data)
            except queue.Full:
                try:
                    self._queue.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._queue.put_nowait(in_data)
                except queue.Full:
                    pass
            return (None, pyaudio.paContinue)

        try:
            self._stream = pa.open(
                input=True,
                start=False,
                format=pyaudio.paInt16,
                channels=int(self._cfg.raw_channels),
                rate=int(self._cfg.sample_rate),
                frames_per_buffer=int(frames_per_buffer),
                stream_callback=_callback,
                input_device_index=self._device_index,
            )
            self._stream.start_stream()
            self._pa = pa
            log.info(
                "ReSpeakerFlexInput: official pipeline ready (PyAudio) dev=%s rate=%d ch=%d proc=%d",
                self._device_index, self._cfg.sample_rate, self._cfg.raw_channels, self._cfg.processed_channel,
            )
            return True
        except Exception as exc:
            try:
                pa.terminate()
            except Exception:
                pass
            self._stream = None
            self._pa = None
            log.warning("ReSpeaker official pipeline open failed: %s", exc)
            return False

    def _init_sounddevice(self) -> bool:
        if self._cfg.device_index is not None:
            self._device_index = self._cfg.device_index
        elif self._cfg.device_name:
            self._device_index = self._find_device()
            if self._device_index is None:
                log.warning(
                    "[sim] ReSpeaker legacy fallback: no device matched %r",
                    self._cfg.device_name,
                )
                return False

        if hasattr(sd, "check_input_settings"):
            try:
                sd.check_input_settings(
                    device=self._device_index,
                    channels=self._cfg.raw_channels,
                    samplerate=self._cfg.sample_rate,
                    dtype="float32",
                )
                log.info(
                    "ReSpeakerFlexInput: legacy fallback ready (sounddevice) dev=%s rate=%d ch=%d proc=%d",
                    self._device_index, self._cfg.sample_rate, self._cfg.raw_channels, self._cfg.processed_channel,
                )
                return True
            except Exception as exc:
                log.warning("ReSpeaker legacy fallback probe failed: %s", exc)
                return False
        return False

    def _find_device(self) -> Optional[int]:
        """Return index of the first input device whose name contains device_name."""
        if not _SD_AVAILABLE:
            return None
        try:
            devices = sd.query_devices()
        except Exception as exc:
            log.warning("query_devices failed: %s", exc)
            return None
        needle = self._cfg.device_name.lower()
        for idx, dev in enumerate(devices):
            if dev.get("max_input_channels", 0) <= 0:
                continue
            if needle in dev.get("name", "").lower():
                log.debug(
                    "ReSpeaker input matched: index=%d name=%r",
                    idx, dev["name"],
                )
                return idx
        return None

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def device_index(self) -> Optional[int]:
        return self._device_index

    @property
    def device_info(self) -> Optional[dict]:
        if self._sim or not _SD_AVAILABLE:
            return None
        try:
            return dict(sd.query_devices(
                self._device_index if self._device_index is not None else None,
                kind="input",
            ))
        except Exception:
            return None

    def record(self, seconds: float) -> np.ndarray:
        """Record *seconds* of audio and return the processed channel as float32.

        Returns silence (zeros, shape ``(n_samples,)``) in sim mode.
        """
        n_samples = int(seconds * self._cfg.sample_rate)
        if self._sim:
            return np.zeros(n_samples, dtype=np.float32)

        if self._mode == "official" and self._stream is not None:
            need_frames = n_samples
            chunks: list[np.ndarray] = []
            got = 0
            deadline = time.monotonic() + seconds + 1.0
            while got < need_frames and time.monotonic() < deadline:
                timeout = max(0.01, min(0.2, deadline - time.monotonic()))
                try:
                    b = self._queue.get(timeout=timeout)
                except queue.Empty:
                    continue
                if not b:
                    continue
                arr = np.frombuffer(b, dtype=np.int16)
                if arr.size == 0:
                    continue
                frames = arr.size // int(self._cfg.raw_channels)
                if frames <= 0:
                    continue
                arr = arr[: frames * int(self._cfg.raw_channels)]
                arr = arr.reshape(frames, int(self._cfg.raw_channels)).astype(np.float32) / 32768.0
                ch = min(int(self._cfg.processed_channel), arr.shape[1] - 1)
                mono = arr[:, ch]
                chunks.append(mono)
                got += mono.size

            if got < need_frames:
                if chunks:
                    out = np.concatenate(chunks, axis=0)
                else:
                    out = np.zeros(0, dtype=np.float32)
                if out.size < need_frames:
                    out = np.pad(out, (0, need_frames - out.size))
                return out[:need_frames]
            out = np.concatenate(chunks, axis=0)
            return out[:need_frames]

        # Legacy sounddevice fallback
        try:
            recording = sd.rec(
                n_samples,
                samplerate=self._cfg.sample_rate,
                channels=self._cfg.raw_channels,
                dtype="float32",
                device=self._device_index,
                blocking=True,
            )
            ch = min(self._cfg.processed_channel, recording.shape[1] - 1)
            return recording[:, ch].copy()
        except Exception as exc:
            log.warning("ReSpeaker record failed: %s", exc)
            return np.zeros(n_samples, dtype=np.float32)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
            except Exception:
                pass
            try:
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None


# ─────────────────────────────────────────────────────────────────────────────
# Output
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ReSpeakerFlexOutputConfig:
    # ALSA device for aplay.  "pulse" routes through PipeWire for mixing.
    # Use "plughw:ReSpeaker,0" to drive the onboard speaker directly
    # (exclusive — other audio sources won't mix in).
    alsa_device: str = "pulse"
    sample_rate: int = 44100
    channels: int = 2
    loudness_boost: float = 2.0
    eq_preset: str = "flat"
    custom_eq_bands: list = field(default_factory=list)


class ReSpeakerFlexOutput:
    """Audio output for the ReSpeaker Flex Linear built-in speaker.

    Delegates to :class:`~src.audio.output.AudioOutput` (aplay pipeline)
    with ReSpeaker-targeted defaults.  Exposes the same API so it is a
    drop-in replacement.
    """

    def __init__(self, config: Optional[ReSpeakerFlexOutputConfig] = None) -> None:
        self._cfg = config or ReSpeakerFlexOutputConfig()

        from src.audio.output import AudioOutput, AudioOutputConfig
        out_cfg = AudioOutputConfig(
            alsa_device=self._cfg.alsa_device,
            sample_rate=self._cfg.sample_rate,
            channels=self._cfg.channels,
            loudness_boost=self._cfg.loudness_boost,
            eq_preset=self._cfg.eq_preset,
            custom_eq_bands=list(self._cfg.custom_eq_bands),
        )
        self._out = AudioOutput(out_cfg)

    # ── Delegate everything to the inner AudioOutput ─────────────────────

    @property
    def hardware_ready(self) -> bool:
        return self._out.hardware_ready

    @property
    def device_index(self) -> Optional[int]:
        return self._out.device_index

    @property
    def device_info(self) -> Optional[dict]:
        return self._out.device_info

    def set_eq_preset(self, preset: str) -> None:
        self._out.set_eq_preset(preset)

    def set_custom_eq_bands(self, bands: list) -> None:
        self._out.set_custom_eq_bands(bands)

    def play(self, samples: np.ndarray, sample_rate: int, blocking: bool = True) -> None:
        self._out.play(samples, sample_rate, blocking=blocking)

    def write_chunk(self, samples: np.ndarray, sample_rate: int) -> None:
        self._out.write_chunk(samples, sample_rate)

    def flush(self) -> None:
        self._out.flush()

    def stop(self) -> None:
        self._out.stop()

    def beep(
        self,
        frequency: float = 440.0,
        duration: float = 0.2,
        amplitude: float = 0.2,
    ) -> None:
        self._out.beep(frequency=frequency, duration=duration, amplitude=amplitude)

    def chime(
        self,
        notes: tuple = (523, 659, 784),
        duration: float = 0.12,
        gap: float = 0.04,
    ) -> None:
        self._out.chime(notes=notes, duration=duration, gap=gap)


# ─────────────────────────────────────────────────────────────────────────────
# LED ring (optional)
# ─────────────────────────────────────────────────────────────────────────────

class ReSpeakerFlexLED:
    """Optional LED ring controller for the ReSpeaker Flex Linear.

    Provides named state changes (idle, listening, speaking, thinking, error).
    Publishes each state change on the ``respeaker.led`` bus topic even when
    the hardware library is not installed.

    Requires one of:
    * ``pip install pixel-ring`` (older ReSpeaker USB products)
    * ``pip install usb-pixel-ring-v2`` (newer USB arrays)

    If neither is present the class operates as a silent no-op.
    """

    def __init__(self, bus=None, enabled: bool = True) -> None:
        self._bus = bus
        self._enabled = enabled and _LED_AVAILABLE
        self._ring = None
        self._state = LED_STATE_IDLE

        if enabled and not _LED_AVAILABLE:
            log.info(
                "ReSpeakerFlexLED: LED library not found — "
                "install pixel-ring or usb-pixel-ring-v2 to enable LEDs"
            )

        if self._enabled:
            self._init_ring()

    def _init_ring(self) -> None:
        try:
            if _LED_LIB == "pixel_ring" and _pixel_ring is not None:
                self._ring = _pixel_ring
                log.info("ReSpeakerFlexLED: using pixel_ring library")
            elif _LED_LIB == "usb_pixel_ring_v2" and _UsbPixelRing is not None:
                self._ring = _UsbPixelRing()
                log.info("ReSpeakerFlexLED: using usb_pixel_ring_v2 library")
        except Exception as exc:
            log.warning("ReSpeakerFlexLED: init failed — %s", exc)
            self._enabled = False
            self._ring = None

    def set_state(self, state: str) -> None:
        """Set the LED ring to *state* (one of the LED_STATE_* constants)."""
        self._state = state
        if self._bus is not None:
            try:
                self._bus.publish("respeaker.led", {"state": state})
            except Exception:
                pass

        if not self._enabled or self._ring is None:
            return

        try:
            if state == LED_STATE_IDLE:
                self._ring.off()
            elif state == LED_STATE_LISTENING:
                self._ring.listen()
            elif state == LED_STATE_SPEAKING:
                self._ring.speak()
            elif state == LED_STATE_THINKING:
                self._ring.think()
            elif state == LED_STATE_ERROR:
                self._ring.set_color(r=255, g=0, b=0)
            else:
                log.debug("ReSpeakerFlexLED: unknown state %r", state)
        except Exception as exc:
            log.debug("ReSpeakerFlexLED: LED command failed: %s", exc)

    @property
    def current_state(self) -> str:
        return self._state
