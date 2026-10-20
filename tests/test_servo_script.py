import importlib.util
from pathlib import Path


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "test_servo.py"
_SPEC = importlib.util.spec_from_file_location("servo_test_script", _SCRIPT_PATH)
assert _SPEC is not None and _SPEC.loader is not None
servo_script = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(servo_script)


class FakeIPCClient:
    instances = []
    next_status = {}
    next_speech_confirmation = True

    def __init__(self, *_args, **_kwargs):
        self.status = {
            "hardware_ready": True,
            "servo_enabled": True,
            "pan_channel": 0,
            "tilt_channel": 1,
            "soft_min_deg": 70,
            "soft_max_deg": 110,
            "tilt_min_deg": 70,
            "tilt_max_deg": 110,
        }
        self.status.update(self.next_status)
        self.positions = {"angle": 90.0, "tilt_angle": 90.0}
        self.payloads = {}
        self.events = []
        self.speech_confirmation = self.next_speech_confirmation
        self.instances.append(self)

    def call(self, request, timeout_ms=None):
        del timeout_ms
        command = request["cmd"]
        if command == "motion.get_status":
            return {"ok": True, "status": self.status}
        if command == "last":
            return {"ok": True, "payload": self.payloads.get(request["topic"])}
        if command == "publish":
            topic = request["topic"]
            payload = request["payload"]
            if topic == "av.say":
                self.events.append(("spoken", payload["text"]))
                if self.speech_confirmation:
                    self.payloads["av.spoke"] = {
                        **payload,
                        "ts": 1,
                    }
            elif topic in ("motion.pan_to", "motion.tilt_to"):
                axis = topic.rsplit(".", 1)[1].removesuffix("_to")
                angle = float(payload["angle"])
                self.events.append(("move", axis, angle))
                key = "angle" if axis == "pan" else "tilt_angle"
                self.positions[key] = angle
                self.payloads["motion.position"] = dict(self.positions)
            return {"ok": True}
        return {"ok": False, "error": f"unexpected command: {command}"}


def _prepare(monkeypatch, argv=None):
    FakeIPCClient.instances.clear()
    FakeIPCClient.next_status = {}
    FakeIPCClient.next_speech_confirmation = True
    monkeypatch.setattr(servo_script, "IPCClient", FakeIPCClient)
    monkeypatch.setattr(servo_script.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        servo_script.sys, "argv", argv or ["test_servo.py"]
    )
    return FakeIPCClient


def test_servo_script_announces_every_configured_axis_move(monkeypatch):
    fake_client = _prepare(monkeypatch)

    assert servo_script.main() == 0

    client = fake_client.instances[0]
    moves = [event for event in client.events if event[0] == "move"]
    assert moves == [
        ("move", "pan", 90),
        ("move", "tilt", 90),
        ("move", "pan", 70),
        ("move", "pan", 90),
        ("move", "pan", 110),
        ("move", "pan", 90),
        ("move", "tilt", 70),
        ("move", "tilt", 90),
        ("move", "tilt", 110),
        ("move", "tilt", 90),
    ]
    assert len(client.events) == 2 * len(moves)
    assert all(
        client.events[index][0] == "spoken"
        and client.events[index + 1][0] == "move"
        for index in range(0, len(client.events), 2)
    )


def test_servo_script_refuses_to_move_without_hardware(monkeypatch, capsys):
    fake_client = _prepare(monkeypatch)
    fake_client.next_status = {"hardware_ready": False}

    assert servo_script.main() == 1
    assert not any(
        event[0] == "move" for event in fake_client.instances[0].events
    )
    assert "hardware is not ready" in capsys.readouterr().err


def test_servo_script_refuses_to_move_when_announcement_is_not_confirmed(
    monkeypatch, capsys
):
    fake_client = _prepare(
        monkeypatch,
        ["test_servo.py", "--speech-timeout", "0.01"],
    )
    fake_client.next_speech_confirmation = False

    assert servo_script.main() == 1
    assert not any(
        event[0] == "move" for event in fake_client.instances[0].events
    )
    assert "no servo move was issued" in capsys.readouterr().err
