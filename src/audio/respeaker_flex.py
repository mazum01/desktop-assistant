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
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

# ── Optional sounddevice import (same pattern as AudioInput) ─────────────────
try:
    import sounddevice as sd
    _SD_AVAILABLE = True
except (ImportError, OSError) as _exc:
    sd = None  # type: ignore
    _SD_AVAILABLE = False
    log.warning("sounddevice not available — ReSpeaker input in sim mode (%s)", _exc)

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
        self._sim = not _SD_AVAILABLE
        self._device_index: Optional[int] = None

        if self._sim:
            return

        if self._cfg.device_index is not None:
            self._device_index = self._cfg.device_index
        elif self._cfg.device_name:
            self._device_index = self._find_device()
            if self._device_index is None:
                log.warning(
                    "[sim] ReSpeaker: no device matched %r — audio input disabled",
                    self._cfg.device_name,
                )
                self._sim = True

        if not self._sim and hasattr(sd, "check_input_settings"):
            try:
                sd.check_input_settings(
                    device=self._device_index,
                    channels=self._cfg.raw_channels,
                    samplerate=self._cfg.sample_rate,
                    dtype="float32",
                )
                log.info(
                    "ReSpeakerFlexInput: device index=%s rate=%d ch=%d proc=%d",
                    self._device_index, self._cfg.sample_rate,
                    self._cfg.raw_channels, self._cfg.processed_channel,
                )
            except Exception as exc:
                log.warning(
                    "[sim] ReSpeaker input probe failed (dev=%s rate=%d ch=%d): %s",
                    self._device_index, self._cfg.sample_rate,
                    self._cfg.raw_channels, exc,
                )
                self._sim = True

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
        if self._sim or not _SD_AVAILABLE:
            return np.zeros(n_samples, dtype=np.float32)

        try:
            recording = sd.rec(
                n_samples,
                samplerate=self._cfg.sample_rate,
                channels=self._cfg.raw_channels,
                dtype="float32",
                device=self._device_index,
                blocking=True,
            )
        except Exception as exc:
            log.warning("ReSpeaker record failed: %s", exc)
            return np.zeros(n_samples, dtype=np.float32)

        # recording shape: (n_samples, raw_channels) — extract processed channel
        ch = min(self._cfg.processed_channel, recording.shape[1] - 1)
        return recording[:, ch].copy()


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
