"""Regression tests for service-owned privacy and depth workers."""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from src.core.bus import MessageBus
from src.services.dense_stereo_service import DenseStereoService
from src.services.mono_depth_service import MonoDepthService
from src.services.privacy_service import PrivacyConfig, PrivacyService


@pytest.fixture
def hardware(monkeypatch):
    monkeypatch.setattr(
        "src.perception.nudity_detector.NudityDetector", MagicMock()
    )
    monkeypatch.setattr(
        "src.services.dense_stereo_service.StereoRectifier", MagicMock()
    )
    monkeypatch.setattr(
        "src.services.dense_stereo_service.DenseStereoMatcher", MagicMock()
    )
    monkeypatch.setattr(
        "src.services.mono_depth_service.resolve_model",
        MagicMock(return_value="/mock/depth.hef"),
    )
    engine = MagicMock()
    engine.hardware_ready = True
    monkeypatch.setattr(
        "src.perception.hailo_inference.HailoInference",
        MagicMock(return_value=engine),
    )
    return engine


@pytest.mark.parametrize("kind", ["privacy", "dense", "mono"])
@pytest.mark.parametrize("enabled", [False, True])
def test_single_owned_worker_stops_and_restarts(kind, enabled, hardware):
    bus = MessageBus()
    if kind == "privacy":
        svc = PrivacyService(bus=bus, config=PrivacyConfig(enabled=enabled))
        process_method = "_check_frame"
    elif kind == "dense":
        svc = DenseStereoService(
            bus=bus, config={"depth": {"dense_enabled": enabled}}
        )
        process_method = "_process_one"
    else:
        svc = MonoDepthService(
            bus=bus, config={"depth": {"mono_enabled": enabled}}
        )
        process_method = "_process_one"

    entered = threading.Event()
    processed = threading.Event()
    loop = svc._run_loop

    def run():
        entered.set()
        loop()

    with patch.object(svc, "_run_loop", side_effect=run), \
         patch.object(svc, process_method, side_effect=processed.set) as process, \
         patch("threading.Thread", wraps=threading.Thread) as threads:
        for cycle in range(2):
            entered.clear()
            processed.clear()
            try:
                svc.start()
                assert entered.wait(2)
                worker = svc._thread
                assert worker is not None
                assert worker.is_alive()
                assert worker.name == f"svc-{svc.name}"
                assert threads.call_count == cycle + 1
                svc.start()
                assert threads.call_count == cycle + 1
                if enabled:
                    assert processed.wait(2)
                else:
                    assert not processed.wait(0.05)
                    process.assert_not_called()
            finally:
                svc.stop()
            assert not worker.is_alive()
            assert not svc.is_running()
            assert svc._unsubs == []
            assert not any(t.name == f"svc-{svc.name}" for t in threading.enumerate())


@pytest.mark.parametrize("model_found", [False, True])
def test_mono_worker_stays_idle_without_ready_hardware(model_found, hardware):
    svc = MonoDepthService(
        bus=MessageBus(), config={"depth": {"mono_enabled": True}}
    )
    hardware.hardware_ready = False
    with patch(
        "src.services.mono_depth_service.resolve_model",
        return_value="/mock/depth.hef" if model_found else None,
    ), patch.object(svc, "_process_one") as process:
        try:
            svc.start()
            assert not svc.hardware_ready
            time.sleep(0.1)
            process.assert_not_called()
        finally:
            svc.stop()
