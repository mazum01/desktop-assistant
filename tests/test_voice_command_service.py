from __future__ import annotations

import numpy as np

from src.core.bus import MessageBus
from src.services.voice_command_service import VoiceCommandConfig, VoiceCommandService
from src.voice.backends import StreamingSTTBackend


class _FakeCapture:
    def __init__(self):
        self._idx = 0
        self._chunk = None

    def push(self, chunk: np.ndarray) -> None:
        self._chunk = chunk.astype(np.float32, copy=True)
        self._idx += 1

    def chunk_index(self) -> int:
        return self._idx

    def latest_chunk(self):
        return self._chunk


class _OneShotWake:
    def __init__(self):
        self._fired = False

    def process(self, samples: np.ndarray, sample_rate: int) -> bool:  # noqa: ARG002
        if self._fired:
            return False
        self._fired = True
        return True

    def reset(self) -> None:
        return


class _StaticSTT(StreamingSTTBackend):
    def __init__(self, transcript: str):
        self._transcript = transcript
        self.chunks = 0

    def start_stream(self) -> None:
        self.chunks = 0

    def accept_chunk(self, samples: np.ndarray, sample_rate: int) -> str | None:  # noqa: ARG002
        self.chunks += 1
        return None

    def finalize(self) -> str:
        return self._transcript


def test_voice_command_service_dispatches_av_utterance_from_transcript():
    bus = MessageBus()
    cap = _FakeCapture()
    stt = _StaticSTT("tell me a joke")
    svc = VoiceCommandService(
        bus=bus,
        capture_service=cap,
        config=VoiceCommandConfig(
            enabled=True,
            command_min_s=0.0,
            silence_end_s=0.0,
            command_max_s=1.0,
        ),
        wake_detector=_OneShotWake(),
        stt_backend=stt,
    )
    utterances = []
    bus.subscribe("av.utterance", lambda _t, p: utterances.append(p))
    svc.on_start()
    try:
        cap.push(np.ones(800, dtype=np.float32) * 0.05)  # wake
        svc.run_tick()
        cap.push(np.ones(800, dtype=np.float32) * 0.05)  # command
        svc.run_tick()
    finally:
        svc.on_stop()

    assert utterances
    assert utterances[0]["text"] == "tell me a joke"
    assert utterances[0]["source"] == "voice"
    assert stt.chunks == 2


def test_voice_command_service_handles_empty_transcript_without_dispatch():
    bus = MessageBus()
    cap = _FakeCapture()
    stt = _StaticSTT("")
    svc = VoiceCommandService(
        bus=bus,
        capture_service=cap,
        config=VoiceCommandConfig(
            enabled=True,
            command_min_s=0.0,
            silence_end_s=0.0,
            command_max_s=1.0,
        ),
        wake_detector=_OneShotWake(),
        stt_backend=stt,
    )
    utterances = []
    intents = []
    bus.subscribe("av.utterance", lambda _t, p: utterances.append(p))
    bus.subscribe("voice.intent", lambda _t, p: intents.append(p))
    svc.on_start()
    try:
        cap.push(np.ones(800, dtype=np.float32) * 0.05)
        svc.run_tick()
        cap.push(np.ones(800, dtype=np.float32) * 0.05)
        svc.run_tick()
    finally:
        svc.on_stop()

    assert utterances == []
    assert intents
    assert intents[-1]["intent"] == "empty"


def test_voice_command_service_applies_runtime_config_updates():
    bus = MessageBus()
    cap = _FakeCapture()
    svc = VoiceCommandService(
        bus=bus,
        capture_service=cap,
        config=VoiceCommandConfig(enabled=False, stt_backend="null"),
        wake_detector=_OneShotWake(),
        stt_backend=_StaticSTT(""),
    )
    configs = []
    bus.subscribe("voice.config", lambda _t, p: configs.append(p))
    svc.on_start()
    try:
        bus.publish("voice.set_config", {"enabled": True, "stt_backend": "shell", "stt_command": "echo hi"})
    finally:
        svc.on_stop()
    assert configs
    assert configs[-1]["config"]["enabled"] is True
    assert configs[-1]["config"]["stt_backend"] == "shell"
