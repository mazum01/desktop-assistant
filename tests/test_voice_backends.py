import numpy as np

from src.voice.backends import (
    EnergyWakeWordDetector,
    EnergyWakeWordDetectorConfig,
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
