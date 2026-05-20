"""
Tests for ClockAnnouncer and ClockService.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.audio.clock_announcer import (
    ClockAnnouncer,
    _next_half_hour,
    _pick_joke,
    _spoken_time,
)
from src.services.clock_service import ClockService


# ---------------------------------------------------------------------------
# _spoken_time
# ---------------------------------------------------------------------------

class TestSpokenTime:
    def test_top_of_hour_am(self):
        dt = datetime(2024, 1, 1, 9, 0, 0)
        assert _spoken_time(dt) == "It is nine o'clock AM."

    def test_top_of_hour_pm(self):
        dt = datetime(2024, 1, 1, 15, 0, 0)
        assert _spoken_time(dt) == "It is three o'clock PM."

    def test_half_hour(self):
        dt = datetime(2024, 1, 1, 10, 30, 0)
        assert _spoken_time(dt) == "It is ten thirty AM."

    def test_midnight(self):
        dt = datetime(2024, 1, 1, 0, 0, 0)
        assert _spoken_time(dt) == "It is twelve o'clock AM."

    def test_noon(self):
        dt = datetime(2024, 1, 1, 12, 0, 0)
        assert _spoken_time(dt) == "It is twelve o'clock PM."

    def test_twelve_thirty_pm(self):
        dt = datetime(2024, 1, 1, 12, 30, 0)
        assert _spoken_time(dt) == "It is twelve thirty PM."

    def test_single_digit_minute_uses_oh(self):
        dt = datetime(2024, 1, 1, 9, 7, 0)
        assert _spoken_time(dt) == "It is nine oh seven AM."


# ---------------------------------------------------------------------------
# _next_half_hour
# ---------------------------------------------------------------------------

class TestNextHalfHour:
    def test_before_30(self):
        dt = datetime(2024, 1, 1, 10, 10, 0)
        nxt = _next_half_hour(dt)
        assert nxt == datetime(2024, 1, 1, 10, 30, 0)

    def test_after_30(self):
        dt = datetime(2024, 1, 1, 10, 45, 0)
        nxt = _next_half_hour(dt)
        assert nxt == datetime(2024, 1, 1, 11, 0, 0)

    def test_exactly_at_30_boundary(self):
        """Exactly on :30 should step to the next :00."""
        dt = datetime(2024, 1, 1, 10, 30, 0)
        nxt = _next_half_hour(dt)
        assert nxt == datetime(2024, 1, 1, 11, 0, 0)

    def test_spans_midnight(self):
        dt = datetime(2024, 1, 1, 23, 45, 0)
        nxt = _next_half_hour(dt)
        assert nxt == datetime(2024, 1, 2, 0, 0, 0)


# ---------------------------------------------------------------------------
# _pick_joke
# ---------------------------------------------------------------------------

class TestPickJoke:
    def test_returns_string(self):
        joke = _pick_joke()
        assert isinstance(joke, str)
        assert len(joke) > 10

    def test_no_immediate_repeat(self):
        seen = set()
        for _ in range(10):
            seen.add(_pick_joke())
        assert len(seen) > 1


# ---------------------------------------------------------------------------
# ClockAnnouncer._announce
# ---------------------------------------------------------------------------

class TestClockAnnouncerAnnounce:
    def _make_announcer(self, enabled=True, pause_fn=None):
        say = MagicMock()
        ann = ClockAnnouncer(say_fn=say, enabled=enabled, pause_fn=pause_fn)
        return ann, say

    def test_top_of_hour_includes_joke(self):
        pause = MagicMock()
        ann, say = self._make_announcer(pause_fn=pause)
        dt = datetime(2024, 1, 1, 10, 0, 0)
        ann._announce(dt)
        # say_fn called twice: once for time, once for the joke
        assert say.call_count == 2
        time_text = say.call_args_list[0][0][0]
        joke_text = say.call_args_list[1][0][0]
        assert "o'clock" in time_text
        assert len(joke_text) > 10
        # pause was called once between them
        pause.assert_called_once()

    def test_half_hour_no_joke(self):
        ann, say = self._make_announcer()
        dt = datetime(2024, 1, 1, 10, 30, 0)
        ann._announce(dt)
        say.assert_called_once()
        text = say.call_args[0][0]
        assert text == _spoken_time(dt)

    def test_disabled_does_not_speak(self):
        ann, say = self._make_announcer(enabled=False)
        dt = datetime(2024, 1, 1, 10, 0, 0)
        ann._announce(dt)
        say.assert_not_called()

    def test_say_fn_exception_does_not_propagate(self):
        say = MagicMock(side_effect=RuntimeError("audio broken"))
        ann = ClockAnnouncer(say_fn=say, enabled=True)
        dt = datetime(2024, 1, 1, 10, 0, 0)
        ann._announce(dt)  # should not raise


# ---------------------------------------------------------------------------
# ClockAnnouncer start/stop
# ---------------------------------------------------------------------------

class TestClockAnnouncerLifecycle:
    def test_start_stop(self):
        say = MagicMock()
        ann = ClockAnnouncer(say_fn=say, enabled=True)
        ann.start()
        assert ann._thread is not None
        assert ann._thread.is_alive()
        ann.stop()
        # Thread should exit within its 1s sleep chunk + join timeout
        ann._thread.join(timeout=3.0)
        assert not ann._thread.is_alive()

    def test_announce_called_for_top_of_hour(self):
        """_run eventually calls _announce; verify indirectly via direct call."""
        say = MagicMock()
        ann = ClockAnnouncer(say_fn=say, enabled=True)
        ann._announce(datetime(2024, 6, 15, 14, 0, 0))
        # say called twice: time string + joke
        assert say.call_count == 2
        assert "o'clock" in say.call_args_list[0][0][0]


# ---------------------------------------------------------------------------
# ClockService
# ---------------------------------------------------------------------------

class TestClockService:
    def test_on_start_creates_announcer(self):
        bus = MagicMock()
        svc = ClockService(bus=bus, enabled=True)
        svc.on_start()
        assert svc._announcer is not None
        svc.on_stop()

    def test_on_stop_stops_announcer(self):
        bus = MagicMock()
        svc = ClockService(bus=bus, enabled=True)
        svc.on_start()
        ann = svc._announcer
        svc.on_stop()
        ann._thread.join(timeout=3.0)
        assert not ann._thread.is_alive()

    def test_clock_enabled_toggle(self):
        bus = MagicMock()
        svc = ClockService(bus=bus, enabled=True)
        svc.on_start()
        svc.clock_enabled = False
        assert svc._announcer.enabled is False
        svc.clock_enabled = True
        assert svc._announcer.enabled is True
        svc.on_stop()

    def test_say_publishes_to_bus(self):
        bus = MagicMock()
        svc = ClockService(bus=bus, enabled=True)
        svc.on_start()
        ann = svc._announcer
        ann._say("hello test")
        bus.publish.assert_called_with("av.say", {"text": "hello test"})
        svc.on_stop()

    def test_disabled_service_does_not_speak(self):
        bus = MagicMock()
        svc = ClockService(bus=bus, enabled=False)
        svc.on_start()
        ann = svc._announcer
        ann._announce(datetime(2024, 1, 1, 10, 0, 0))
        bus.publish.assert_not_called()
        svc.on_stop()
