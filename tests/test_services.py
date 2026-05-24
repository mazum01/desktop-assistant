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
        self.move_kwargs = []
        self.relaxed = False
        self.stopped_count = 0
        self._cfg = type("Cfg", (), {"speed_deg_per_sec": 90.0})()

    def _write(self, angle):
        self.position = angle

    def move_to(self, angle, **kw):
        self.moves.append(angle)
        self.move_kwargs.append(kw)
        self.position = angle
    def relax(self):    self.relaxed = True
    def stop(self):     self.stopped_count += 1
    def plan_direction(self, a, b):
        return "forward" if b > a else "backward"


def test_motion_service_handles_pan_to():
    bus = MessageBus()
    fake = _FakeServo(start_pos=150.0)
    svc = MotionService(bus=bus, controller=fake)
    svc.start()
    try:
        moved = []
        bus.subscribe("motion.moved", lambda t, p: moved.append(p))
        bus.publish("motion.pan_to", {"angle": 200.0})
        # motion.moved is published immediately (non-blocking new design)
        assert moved and moved[0]["to"] == 200.0
        assert moved[0]["direction"] == "forward"
        # Servo loop converges within ~0.7s (50° at 90°/s = ~0.56s)
        time.sleep(0.7)
        assert abs(fake.position - 200.0) < 2.0
    finally:
        svc.stop()


def test_motion_service_pan_to_with_move_time_sets_speed():
    bus = MessageBus()
    fake = _FakeServo(start_pos=150.0)
    svc = MotionService(bus=bus, controller=fake)
    svc.start()
    try:
        moved = []
        bus.subscribe("motion.moved", lambda t, p: moved.append(p))
        bus.publish("motion.pan_to", {"angle": 210.0, "move_time_ms": 3000})
        assert moved and moved[0]["to"] == 210.0
        # Speed is 20 deg/s (60° in 3s). After 0.1s, should have moved ~2°, not 60°.
        time.sleep(0.1)
        assert fake.position < 210.0  # still stepping toward target
    finally:
        svc.stop()


def test_motion_service_pan_to_with_invalid_move_time_is_ignored():
    bus = MessageBus()
    fake = _FakeServo(start_pos=150.0)
    svc = MotionService(bus=bus, controller=fake)
    svc.start()
    try:
        initial = fake.position
        bus.publish("motion.pan_to", {"angle": 200.0, "move_time_ms": 0})
        bus.publish("motion.pan_to", {"angle": 200.0, "move_time_ms": -10})
        bus.publish("motion.pan_to", {"angle": 200.0, "move_time_ms": "bad"})
        time.sleep(0.05)
        # No target was set, servo should not have moved
        assert abs(fake.position - initial) < 0.1
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

    positions = []
    bus.subscribe("motion.position", lambda t, p: positions.append(p))

    svc.start()
    time.sleep(0.2)  # servo loop publishes position every ~100ms
    svc.stop()

    assert positions and positions[-1]["angle"] == pytest.approx(42.0, abs=0.1)


def test_motion_service_pan_to_ignores_bad_payload():
    bus = MessageBus()
    fake = _FakeServo()
    svc = MotionService(bus=bus, controller=fake)
    svc.start()
    try:
        initial = fake.position
        bus.publish("motion.pan_to", "not a dict")
        bus.publish("motion.pan_to", {"wrong_key": 1})
        time.sleep(0.05)
        assert abs(fake.position - initial) < 0.1
    finally:
        svc.stop()


# ── AV ───────────────────────────────────────────────────────────────────

def _make_av(bus, announce_on_start=False, audio_input=None):
    import numpy as np
    audio = MagicMock(hardware_ready=True)
    tts = MagicMock(hardware_ready=True)
    # render() must return (samples, sr) so the async synthesis path works.
    tts.render.return_value = (np.zeros(100, dtype=np.float32), 22050)
    announcer = MagicMock()
    announcer.maybe_handle.return_value = False
    svc = AVService(
        bus=bus,
        audio_output=audio,
        tts=tts,
        audio_input=audio_input,
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
        # New async path: render() is called for synthesis, then audio.play() for playback.
        tts.render.assert_called_once_with("hello world")
        audio.play.assert_called_once()
        assert spoke and spoke[0]["text"] == "hello world"
    finally:
        svc.stop()


def test_av_service_announces_version_on_startup():
    bus = MessageBus()
    svc, audio, tts, announcer = _make_av(bus, announce_on_start=True)
    svc.start()
    try:
        svc.wait_idle(timeout=10.0)
        # New async path: startup phrase is pre-synthesized via render(), then
        # played via audio.play(). The announcer is bypassed on the happy path.
        tts.render.assert_called()
        audio.play.assert_called()
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
        audio.beep.assert_called_once_with(frequency=440.0, duration=0.1)
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


class _FakeMic:
    class _Cfg:
        sample_rate = 16000

    def __init__(self):
        self._cfg = self._Cfg()
        self.hardware_ready = True

    def record(self, seconds):
        import numpy as np
        n = int(self._cfg.sample_rate * float(seconds))
        t = np.linspace(0.0, float(seconds), n, endpoint=False)
        return (0.2 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


class _FakeSilentMic:
    class _Cfg:
        sample_rate = 16000

    def __init__(self):
        self._cfg = self._Cfg()
        self.hardware_ready = True

    def record(self, seconds):
        import numpy as np
        n = int(self._cfg.sample_rate * float(seconds))
        return np.zeros(n, dtype=np.float32)


def test_av_service_record_clip_writes_wav(tmp_path):
    bus = MessageBus()
    mic = _FakeMic()
    svc, _, _, _ = _make_av(bus, audio_input=mic)
    svc.start()
    try:
        out_path = tmp_path / "clip.wav"
        events = []
        bus.subscribe("av.recorded", lambda t, p: events.append(p))
        bus.publish("av.record", {"seconds": 0.1, "path": str(out_path)})
        svc.wait_idle()
        assert out_path.exists()
        assert events
        assert events[0]["path"] == str(out_path)
    finally:
        svc.stop()


def test_av_service_playback_clip_uses_audio_output(tmp_path):
    import wave
    import numpy as np

    bus = MessageBus()
    svc, audio, _, _ = _make_av(bus)
    svc.start()
    try:
        clip = tmp_path / "clip.wav"
        samples = (0.2 * np.sin(2.0 * np.pi * 220.0 * np.linspace(0, 0.1, 1600, endpoint=False))).astype(np.float32)
        pcm = (samples * 32767.0).astype(np.int16)
        with wave.open(str(clip), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(16000)
            wf.writeframes(pcm.tobytes())

        bus.publish("av.play_recording", {"path": str(clip)})
        svc.wait_idle()
        assert audio.play.called
        _, kwargs = audio.play.call_args
        assert kwargs.get("apply_processing") is False
    finally:
        svc.stop()


def test_av_service_record_clip_fails_on_silence(tmp_path):
    bus = MessageBus()
    mic = _FakeSilentMic()
    svc, _, _, _ = _make_av(bus, audio_input=mic)
    svc.start()
    try:
        out_path = tmp_path / "silent.wav"
        with pytest.raises(RuntimeError, match="recorded silence"):
            svc.record_clip(seconds=0.1, path=str(out_path))
        assert not out_path.exists()
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
        svc.wait_idle()
        tts.render.assert_not_called()
    finally:
        svc.stop()
