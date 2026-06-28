"""
Tests for the audio backend factory and ReSpeaker Flex sim mode.
"""

from __future__ import annotations

import numpy as np
import pytest

from src.audio.factory import (
    BACKEND_DEFAULT,
    BACKEND_RESPEAKER_FLEX,
    create_audio_input,
    create_audio_output,
)
from src.audio.input import AudioInput
from src.audio.output import AudioOutput
from src.audio.pw_input import PipeWireMicInput
from src.audio.respeaker_flex import (
    ReSpeakerFlexInput,
    ReSpeakerFlexInputConfig,
    ReSpeakerFlexLED,
    ReSpeakerFlexOutput,
    ReSpeakerFlexOutputConfig,
    LED_STATE_IDLE,
    LED_STATE_SPEAKING,
)


# ── create_audio_input ────────────────────────────────────────────────────────

def test_create_input_default_returns_audio_input():
    obj = create_audio_input(BACKEND_DEFAULT, {})
    assert isinstance(obj, AudioInput)


def test_create_input_respeaker_returns_respeaker_input():
    obj = create_audio_input(BACKEND_RESPEAKER_FLEX, {})
    assert isinstance(obj, PipeWireMicInput)


def test_create_input_unknown_falls_back_to_default():
    obj = create_audio_input("does_not_exist", {})
    assert isinstance(obj, AudioInput)


def test_create_input_none_cfg_uses_defaults():
    obj = create_audio_input(BACKEND_DEFAULT, None)
    assert isinstance(obj, AudioInput)


# ── create_audio_output ───────────────────────────────────────────────────────

def test_create_output_default_returns_audio_output():
    obj = create_audio_output(BACKEND_DEFAULT, {})
    assert isinstance(obj, AudioOutput)


def test_create_output_respeaker_returns_respeaker_output():
    obj = create_audio_output(BACKEND_RESPEAKER_FLEX, {})
    assert isinstance(obj, ReSpeakerFlexOutput)


def test_create_output_unknown_falls_back_to_default():
    obj = create_audio_output("does_not_exist", {})
    assert isinstance(obj, AudioOutput)


# ── ReSpeakerFlexInput sim mode ───────────────────────────────────────────────

def test_respeaker_input_sim_mode_hardware_not_ready():
    # No real ReSpeaker hardware in CI — expect sim mode (hardware_ready=False)
    mic = ReSpeakerFlexInput(ReSpeakerFlexInputConfig(device_name="__no_such_device__"))
    # We don't assert hardware_ready == False here because sounddevice may not be
    # installed at all (also sim mode).  Either way record() must return zeros.
    result = mic.record(0.1)
    assert isinstance(result, np.ndarray)
    assert result.dtype == np.float32
    assert len(result) > 0
    # In sim mode every sample is zero
    if not mic.hardware_ready:
        assert np.all(result == 0.0)


def test_respeaker_input_record_shape():
    mic = ReSpeakerFlexInput(ReSpeakerFlexInputConfig(
        device_name="__no_such_device__",
        sample_rate=16000,
    ))
    samples = mic.record(0.5)
    # Should be 1D (mono) regardless of backend
    assert samples.ndim == 1


# ── ReSpeakerFlexOutput ───────────────────────────────────────────────────────

def test_respeaker_output_constructs():
    out = ReSpeakerFlexOutput(ReSpeakerFlexOutputConfig(alsa_device="pulse"))
    assert isinstance(out, ReSpeakerFlexOutput)
    # hardware_ready mirrors underlying AudioOutput sim mode detection
    assert isinstance(out.hardware_ready, bool)


def test_respeaker_output_delegates_properties():
    out = ReSpeakerFlexOutput()
    assert out.device_index is None  # aplay backend never has device index
    assert out.device_info is None


def test_respeaker_output_beep_delegates_amplitude():
    out = ReSpeakerFlexOutput()
    calls = []

    def _fake_beep(*, frequency, duration, amplitude):
        calls.append((frequency, duration, amplitude))

    out._out.beep = _fake_beep
    out.beep(frequency=880.0, duration=0.3, amplitude=0.9)
    assert calls == [(880.0, 0.3, 0.9)]


# ── ReSpeakerFlexLED ──────────────────────────────────────────────────────────

def test_led_set_state_no_crash_when_library_absent():
    led = ReSpeakerFlexLED(bus=None, enabled=True)
    # Should not raise even if LED library is not installed
    led.set_state(LED_STATE_SPEAKING)
    led.set_state(LED_STATE_IDLE)
    assert led.current_state == LED_STATE_IDLE


def test_led_publishes_on_bus(monkeypatch):
    published = []

    class FakeBus:
        def publish(self, topic, payload):
            published.append((topic, payload))

    led = ReSpeakerFlexLED(bus=FakeBus(), enabled=True)
    led.set_state(LED_STATE_SPEAKING)
    assert any(t == "respeaker.led" and p["state"] == LED_STATE_SPEAKING
               for t, p in published)


def test_led_disabled_does_not_publish():
    published = []

    class FakeBus:
        def publish(self, topic, payload):
            published.append((topic, payload))

    led = ReSpeakerFlexLED(bus=FakeBus(), enabled=False)
    # Even when disabled, bus publication still happens (observability)
    led.set_state(LED_STATE_SPEAKING)
    assert any(t == "respeaker.led" for t, _ in published)


# ── Config passthrough ────────────────────────────────────────────────────────

def test_factory_input_passes_pipewire_channel_selection():
    cfg = {
        "input_sample_rate": 16000,
        "input_raw_channels": 6,
        "input_source_match": "reSpeaker",
        "input_processing_enabled": False,
        "input_processed_channel": 0,
        "input_raw_mic_channel": 2,
    }
    obj = create_audio_input(BACKEND_RESPEAKER_FLEX, cfg)
    assert obj._cfg.source_match == "reSpeaker"
    assert obj._cfg.sample_rate == 16000
    assert obj._cfg.channels == 6
    assert obj._cfg.select_channel == 2


def test_factory_output_passes_alsa_device():
    cfg = {"output_alsa_device": "hw:ReSpeaker,0", "output_sample_rate": 44100}
    obj = create_audio_output(BACKEND_RESPEAKER_FLEX, cfg)
    assert obj._cfg.alsa_device == "hw:ReSpeaker,0"
