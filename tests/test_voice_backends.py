import numpy as np
import subprocess
import sys
import types

from src.voice.backends import (
    EnergyWakeWordDetector,
    EnergyWakeWordDetectorConfig,
    FasterWhisperSTT,
    FasterWhisperSTTConfig,
    NullStreamingSTT,
    ShellCommandSTT,
    ShellCommandSTTConfig,
)


def test_energy_wake_detector_triggers_after_consecutive_hits():
    det = EnergyWakeWordDetector(
        EnergyWakeWordDetectorConfig(threshold_dbfs=-30.0, consecutive_frames=2)
    )
    quiet = np.zeros(1600, dtype=np.float32)
    loud = np.full(1600, 0.08, dtype=np.float32)
    assert det.process(quiet, 16000) is False
    assert det.process(loud, 16000) is False
    assert det.process(loud, 16000) is True


def test_null_stt_returns_empty():
    stt = NullStreamingSTT()
    stt.start_stream()
    stt.accept_chunk(np.ones(800, dtype=np.float32), 16000)
    assert stt.finalize() == ""


def test_shell_stt_without_command_returns_empty():
    stt = ShellCommandSTT(ShellCommandSTTConfig(command=""))
    stt.start_stream()
    stt.accept_chunk(np.ones(800, dtype=np.float32), 16000)
    assert stt.finalize() == ""


def test_shell_stt_timeout_returns_empty(monkeypatch):
    stt = ShellCommandSTT(ShellCommandSTTConfig(command="echo hello", timeout_s=0.1))
    stt.start_stream()
    stt.accept_chunk(np.ones(800, dtype=np.float32), 16000)
    monkeypatch.setattr(
        "src.voice.backends.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="x", timeout=0.1)),
    )
    assert stt.finalize() == ""


def test_faster_whisper_stt_uses_persistent_model(monkeypatch):
    calls = {}

    class _FakeModel:
        def __init__(self, model, device, compute_type, **kwargs):
            calls["init"] = (model, device, compute_type)
            calls["init_kwargs"] = kwargs

        def transcribe(self, wav_path, **kwargs):
            calls["transcribe"] = (wav_path, kwargs)
            seg = types.SimpleNamespace(text=" what time is it ")
            return [seg], {}

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _FakeModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)

    stt = FasterWhisperSTT(
        FasterWhisperSTTConfig(
            sample_rate=16000,
            language="en",
            model="base.en",
            device="cpu",
            compute_type="int8",
        )
    )
    stt.start_stream()
    stt.accept_chunk(np.ones(800, dtype=np.float32), 16000)
    assert stt.finalize() == "what time is it"
    assert calls["init"] == ("base.en", "cpu", "int8")
    assert calls["transcribe"][1]["language"] == "en"
