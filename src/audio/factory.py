"""
Audio backend factory.

Select audio input/output implementations at runtime based on the
``audio.backend`` key in ``config/assistant.yaml``.

Supported backends
------------------
``default``
    Standard :class:`~src.audio.input.AudioInput` (sounddevice/PortAudio) and
    :class:`~src.audio.output.AudioOutput` (aplay → PipeWire).  Zero behavior
    change from the pre-factory setup.

``respeaker_flex``
    PipeWire-native multi-channel capture from the ReSpeaker Flex Linear USB
    mic array (extracting the configured processed/raw channel) and
    :class:`~src.audio.respeaker_flex.ReSpeakerFlexOutput` for output to its
    built-in speaker.

Usage
-----
::

    from src.audio.factory import create_audio_input, create_audio_output

    audio_cfg = config.get("audio", {})
    backend = audio_cfg.get("backend", BACKEND_DEFAULT)
    backend_cfg = audio_cfg.get(backend, {})

    mic = create_audio_input(backend, backend_cfg)
    out = create_audio_output(backend, backend_cfg)
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

BACKEND_DEFAULT       = "default"
BACKEND_RESPEAKER_FLEX = "respeaker_flex"

_KNOWN_BACKENDS = {BACKEND_DEFAULT, BACKEND_RESPEAKER_FLEX}


def create_audio_input(backend: str, cfg: dict | None = None) -> Any:
    """Return an audio-input object for *backend*.

    Parameters
    ----------
    backend:
        One of :data:`BACKEND_DEFAULT` or :data:`BACKEND_RESPEAKER_FLEX`.
        Unknown values fall back to ``default`` with a warning.
    cfg:
        Backend-specific config dict (the ``audio.<backend>`` sub-section
        from ``assistant.yaml``).  Pass ``{}`` or ``None`` to use all defaults.
    """
    cfg = cfg or {}
    if backend not in _KNOWN_BACKENDS:
        log.warning(
            "Unknown audio backend %r — falling back to %r",
            backend, BACKEND_DEFAULT,
        )
        backend = BACKEND_DEFAULT

    if backend == BACKEND_RESPEAKER_FLEX:
        from src.audio.pw_input import PipeWireMicInput, PipeWireMicConfig
        processing_enabled = bool(cfg.get("input_processing_enabled", True))
        selected_channel = int(
            cfg.get(
                "input_processed_channel" if processing_enabled else "input_raw_mic_channel",
                0 if processing_enabled else 1,
            )
        )
        input_cfg = PipeWireMicConfig(
            sample_rate=int(cfg.get("input_sample_rate", 16000)),
            channels=int(cfg.get("input_raw_channels", 6)),
            select_channel=selected_channel,
            source_match=str(cfg.get("input_source_match", "reSpeaker")),
        )
        log.info(
            "Audio backend: respeaker_flex — input via PipeWire rate=%d raw_ch=%d channel=%d processing=%s match=%r",
            input_cfg.sample_rate, input_cfg.channels,
            input_cfg.select_channel, processing_enabled, input_cfg.source_match,
        )
        return PipeWireMicInput(input_cfg)

    # default
    from src.audio.input import AudioInput, AudioInputConfig
    # "pipewire" selects the PipeWire-native capture path (pw-record subprocess),
    # which is the robust way to read the PipeWire-owned reSpeaker mic array.
    if str(cfg.get("input_device_name", "")).lower() == "pipewire":
        from src.audio.pw_input import PipeWireMicInput, PipeWireMicConfig
        pw_cfg = PipeWireMicConfig(
            sample_rate=int(cfg.get("input_sample_rate", 16000)),
            channels=1,
            source_match=str(cfg.get("input_source_match", "reSpeaker")),
        )
        log.info(
            "Audio backend: default — input via PipeWire (pw-record) rate=%d match=%r",
            pw_cfg.sample_rate, pw_cfg.source_match,
        )
        return PipeWireMicInput(pw_cfg)

    input_cfg = AudioInputConfig(
        device_name=str(cfg.get("input_device_name", "")),
        sample_rate=int(cfg.get("input_sample_rate", 44100)),
        channels=1,
    )
    log.info(
        "Audio backend: default — input device=%r rate=%d",
        input_cfg.device_name or "(system default)", input_cfg.sample_rate,
    )
    return AudioInput(input_cfg)


def create_audio_output(backend: str, cfg: dict | None = None) -> Any:
    """Return an audio-output object for *backend*.

    Parameters
    ----------
    backend:
        One of :data:`BACKEND_DEFAULT` or :data:`BACKEND_RESPEAKER_FLEX`.
        Unknown values fall back to ``default`` with a warning.
    cfg:
        Backend-specific config dict.
    """
    cfg = cfg or {}
    if backend not in _KNOWN_BACKENDS:
        log.warning(
            "Unknown audio backend %r — falling back to %r",
            backend, BACKEND_DEFAULT,
        )
        backend = BACKEND_DEFAULT

    if backend == BACKEND_RESPEAKER_FLEX:
        from src.audio.respeaker_flex import ReSpeakerFlexOutput, ReSpeakerFlexOutputConfig
        output_cfg = ReSpeakerFlexOutputConfig(
            alsa_device=str(cfg.get("output_alsa_device", "pulse")),
            sample_rate=int(cfg.get("output_sample_rate", 44100)),
            loudness_boost=float(cfg.get("loudness_boost", 2.0)),
            eq_preset=str(cfg.get("eq_preset", "flat")),
        )
        log.info(
            "Audio backend: respeaker_flex — output alsa_device=%r rate=%d",
            output_cfg.alsa_device, output_cfg.sample_rate,
        )
        return ReSpeakerFlexOutput(output_cfg)

    # default
    from src.audio.output import AudioOutput, AudioOutputConfig
    output_cfg = AudioOutputConfig(
        alsa_device=str(cfg.get("output_alsa_device", "pulse")),
        sample_rate=int(cfg.get("output_sample_rate", 44100)),
        loudness_boost=float(cfg.get("loudness_boost", 2.0)),
        eq_preset=str(cfg.get("eq_preset", "flat")),
    )
    log.info(
        "Audio backend: default — output alsa_device=%r rate=%d",
        output_cfg.alsa_device, output_cfg.sample_rate,
    )
    return AudioOutput(output_cfg)
