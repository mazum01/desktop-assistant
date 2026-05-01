"""Tests for thermal/motion/AV services with mocked drivers."""

import time
from unittest.mock import MagicMock

import pytest

from src.core.bus import MessageBus
from src.services.thermal_service import ThermalService
from src.services.motion_service import MotionService
from src.services.av_service import AVService


# ── Thermal ──────────────────────────────────────────────────────────────

class _FakeThermalManager:
    def __init__(self, temp=42.0, ok=True, duty=50.0):
        self.temperature_c = temp
        self.sensor_ok = ok
        self.fan_duty = duty
        self.started = False
        self.stopped = False
        self._thresholds = MagicMock(critical_c=75.0)
    def start(self):  self.started = True
    def stop(self):   self.stopped = True


def test_thermal_service_publishes_telemetry():
    bus = MessageBus()
    fake = _FakeThermalManager(temp=42.0)
    svc = ThermalService(bus=bus, manager=fake)

    temps = []
    fans = []
    bus.subscribe("thermal.temp", lambda t, p: temps.append(p))
    bus.subscribe("thermal.fan", lambda t, p: fans.append(p))

    svc.tick_seconds = 0.02
    svc.start()
    time.sleep(0.1)
    svc.stop()

    assert fake.started and fake.stopped
    assert temps and temps[-1]["celsius"] == 42.0
    assert temps[-1]["fahrenheit"] == pytest.approx(107.6, abs=0.1)
    assert fans and fans[-1]["duty"] == 50.0


def test_thermal_service_publishes_critical_once():
    bus = MessageBus()
    fake = _FakeThermalManager(temp=80.0)
    svc = ThermalService(bus=bus, manager=fake)

    crits = []
    bus.subscribe("thermal.critical", lambda t, p: crits.append(p))

    svc.tick_seconds = 0.02
    svc.start()
    time.sleep(0.1)
    svc.stop()

    # Edge-triggered: should fire on first hot tick, not every tick.
    assert len(crits) == 1
    assert crits[0]["celsius"] == 80.0


def test_thermal_service_publishes_error_on_no_reading():
    bus = MessageBus()
    fake = _FakeThermalManager()
    fake.temperature_c = None
    svc = ThermalService(bus=bus, manager=fake)

    errs = []
    bus.subscribe("thermal.error", lambda t, p: errs.append(p))

    svc.tick_seconds = 0.02
    svc.start()
    time.sleep(0.1)
    svc.stop()

    assert errs and errs[0]["reason"] == "no_reading"


# ── Motion ───────────────────────────────────────────────────────────────

class _FakeServo:
    def __init__(self, start_pos=180.0):
        self.position = start_pos
        self.hardware_ready = True
        self.moves = []
        self.relaxed = False
        self.stopped_count = 0
    def move_to(self, angle, **kw):
        self.moves.append(angle)
        self.position = angle
    def relax(self):    self.relaxed = True
    def stop(self):     self.stopped_count += 1
    def plan_direction(self, a, b):
        return "forward" if b > a else "backward"


def test_motion_service_handles_pan_to():
    bus = MessageBus()
    fake = _FakeServo(start_pos=10.0)
    svc = MotionService(bus=bus, controller=fake)
    svc.tick_seconds = 1.0  # don't tick during test
    svc.start()
    try:
        moved = []
        bus.subscribe("motion.moved", lambda t, p: moved.append(p))
        bus.publish("motion.pan_to", {"angle": 90.0})
        assert fake.moves == [90.0]
        assert moved and moved[0]["to"] == 90.0
        assert moved[0]["direction"] == "forward"
    finally:
        svc.stop()


def test_motion_service_relax_and_stop_cmds():
    bus = MessageBus()
    fake = _FakeServo()
    svc = MotionService(bus=bus, controller=fake)
    svc.tick_seconds = 1.0
    svc.start()
    try:
        bus.publish("motion.relax", None)
        assert fake.relaxed
        bus.publish("motion.stop", None)
        assert fake.stopped_count >= 1
    finally:
        svc.stop()


def test_motion_service_publishes_position():
    bus = MessageBus()
    fake = _FakeServo(start_pos=42.0)
    svc = MotionService(bus=bus, controller=fake)
    svc.tick_seconds = 0.02

    positions = []
    bus.subscribe("motion.position", lambda t, p: positions.append(p))

    svc.start()
    time.sleep(0.1)
    svc.stop()

    assert positions and positions[-1]["angle"] == 42.0


def test_motion_service_pan_to_ignores_bad_payload():
    bus = MessageBus()
    fake = _FakeServo()
    svc = MotionService(bus=bus, controller=fake)
    svc.tick_seconds = 1.0
    svc.start()
    try:
        bus.publish("motion.pan_to", "not a dict")
        bus.publish("motion.pan_to", {"wrong_key": 1})
        assert fake.moves == []
    finally:
        svc.stop()


# ── AV ───────────────────────────────────────────────────────────────────

def _make_av(bus, announce_on_start=False):
    audio = MagicMock(hardware_ready=True)
    tts = MagicMock(hardware_ready=True)
    announcer = MagicMock()
    announcer.maybe_handle.return_value = False
    svc = AVService(
        bus=bus,
        audio_output=audio,
        tts=tts,
        announcer=announcer,
        announce_on_start=announce_on_start,
    )
    svc.tick_seconds = 1.0
    return svc, audio, tts, announcer


def test_av_service_say_routes_to_tts():
    bus = MessageBus()
    svc, audio, tts, _ = _make_av(bus)
    svc.start()
    try:
        spoke = []
        bus.subscribe("av.spoke", lambda t, p: spoke.append(p))
        bus.publish("av.say", {"text": "hello world"})
        svc.wait_idle()
        tts.say.assert_called_once_with("hello world", output=audio)
        assert spoke and spoke[0]["text"] == "hello world"
    finally:
        svc.stop()


def test_av_service_announces_version_on_startup():
    bus = MessageBus()
    svc, _, _, announcer = _make_av(bus, announce_on_start=True)
    svc.start()
    try:
        svc.wait_idle()
        announcer.announce_startup.assert_called_once()
    finally:
        svc.stop()


def test_av_service_utterance_invokes_version_announcer():
    bus = MessageBus()
    svc, _, _, announcer = _make_av(bus)
    announcer.maybe_handle.return_value = True
    svc.start()
    try:
        bus.publish("av.utterance", {"text": "what version are you"})
        svc.wait_idle()
        announcer.maybe_handle.assert_called_with("what version are you")
    finally:
        svc.stop()


def test_av_service_announce_version_topic():
    bus = MessageBus()
    svc, _, _, announcer = _make_av(bus)
    svc.start()
    try:
        bus.publish("av.announce_version", None)
        svc.wait_idle()
        announcer.announce_on_request.assert_called_once()
    finally:
        svc.stop()


def test_av_service_beep():
    bus = MessageBus()
    svc, audio, _, _ = _make_av(bus)
    svc.start()
    try:
        bus.publish("av.beep", {"freq": 440, "duration": 0.1})
        svc.wait_idle()
        audio.beep.assert_called_once_with(freq=440.0, duration=0.1)
    finally:
        svc.stop()


def test_av_service_chime_default():
    bus = MessageBus()
    svc, audio, _, _ = _make_av(bus)
    svc.start()
    try:
        bus.publish("av.chime", {})
        svc.wait_idle()
        audio.chime.assert_called_once_with()
        assert bus.last("av.chimed") == {}
    finally:
        svc.stop()


def test_av_service_chime_with_overrides():
    bus = MessageBus()
    svc, audio, _, _ = _make_av(bus)
    svc.start()
    try:
        bus.publish("av.chime", {"notes": [440, 550], "note_duration": 0.1})
        svc.wait_idle()
        audio.chime.assert_called_once_with(notes=(440.0, 550.0), note_duration=0.1)
    finally:
        svc.stop()


def test_av_service_say_ignores_empty():
    bus = MessageBus()
    svc, _, tts, _ = _make_av(bus)
    svc.start()
    try:
        bus.publish("av.say", {"text": ""})
        bus.publish("av.say", {})
        tts.say.assert_not_called()
    finally:
        svc.stop()
