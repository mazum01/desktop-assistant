"""Smoke tests for the split-unit entry points."""

import os
import signal
import threading
import time

import pytest

from src.assistant import core_main, thermal_main
from src.assistant.runner import run_services
from src.core.bus import MessageBus
from src.core.service import Service


class _DummyService(Service):
    name = "dummy"
    tick_seconds = 0.05

    def __init__(self, **kw):
        super().__init__(**kw)
        self.ticks = 0

    def run_tick(self):
        self.ticks += 1


def _send_sigint_after(delay: float) -> None:
    def fire():
        time.sleep(delay)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=fire, daemon=True).start()


def test_run_services_starts_stops_and_returns_zero():
    bus = MessageBus()
    svc = _DummyService(bus=bus)
    _send_sigint_after(0.2)
    rc = run_services([svc], unit_name="test")
    assert rc == 0
    assert svc.ticks >= 1
    assert not svc.is_running()


def test_run_services_returns_1_when_no_service_starts():
    class Failing(Service):
        name = "failing"
        def on_start(self):
            raise RuntimeError("nope")

    rc = run_services([Failing(bus=MessageBus())], unit_name="test")
    assert rc == 1


def test_thermal_main_module_is_importable():
    # Ensure the entry point imports cleanly (no top-level hardware access).
    assert callable(thermal_main.main)


def test_core_main_module_is_importable():
    assert callable(core_main.main)
