"""Tests for src.core.service.Service base class."""

import threading
import time

import pytest

from src.core.bus import MessageBus
from src.core.service import Service


class _CountingService(Service):
    name = "counter"
    tick_seconds = 0.02

    def __init__(self, **kw):
        super().__init__(**kw)
        self.started = False
        self.stopped = False
        self.tick_count = 0
        self._lock = threading.Lock()

    def on_start(self):
        self.started = True

    def run_tick(self):
        with self._lock:
            self.tick_count += 1

    def on_stop(self):
        self.stopped = True


def test_start_invokes_on_start_and_publishes_event():
    bus = MessageBus()
    events = []
    bus.subscribe("service.started", lambda t, p: events.append(p))
    svc = _CountingService(bus=bus)
    svc.start()
    try:
        assert svc.is_running()
        assert svc.started
        assert events and events[0]["name"] == "counter"
    finally:
        svc.stop()


def test_run_tick_called_repeatedly():
    bus = MessageBus()
    svc = _CountingService(bus=bus)
    svc.start()
    time.sleep(0.15)
    svc.stop()
    assert svc.tick_count >= 3, f"expected ≥3 ticks, got {svc.tick_count}"


def test_stop_invokes_on_stop_and_publishes_event():
    bus = MessageBus()
    events = []
    bus.subscribe("service.stopped", lambda t, p: events.append(p))
    svc = _CountingService(bus=bus)
    svc.start()
    svc.stop()
    assert svc.stopped
    assert not svc.is_running()
    assert events and events[0]["name"] == "counter"


def test_double_start_is_noop():
    svc = _CountingService(bus=MessageBus())
    svc.start()
    svc.start()  # should warn, not crash
    svc.stop()


def test_stop_without_start_is_safe():
    svc = _CountingService(bus=MessageBus())
    svc.stop()  # no exception


def test_context_manager_starts_and_stops():
    svc = _CountingService(bus=MessageBus())
    with svc:
        assert svc.is_running()
    assert not svc.is_running()
    assert svc.stopped


def test_tick_exception_is_logged_not_raised():
    class Bad(Service):
        name = "bad"
        tick_seconds = 0.02
        def run_tick(self):
            raise RuntimeError("boom")

    svc = Bad(bus=MessageBus())
    svc.start()
    time.sleep(0.1)
    svc.stop()  # must not raise
