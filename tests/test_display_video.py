"""Video commands must report device results, not just successful bus delivery."""

import ast
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

from src.core.bus import MessageBus
from src.services.display_service import DisplayService, DisplayServiceConfig


def service():
    bus = MessageBus()
    svc = DisplayService(bus=bus, config=DisplayServiceConfig(
        ble_enabled=True, ble_address="28:84:85:88:1B:25",
        ble_characteristic_uuid="command",
        ble_status_characteristic_uuid="status",
    ))
    replies, errors = [], []
    bus.subscribe("display.reply", lambda _t, p: replies.append(p))
    bus.subscribe("display.error", lambda _t, p: errors.append(p))
    return svc, replies, errors


def test_video_command_preserves_correlation_and_only_allowed_fields():
    svc, replies, errors = service()
    svc._on_video("", {"action": "play", "path": "/videos/test.mjpg", "fps": 12,
                       "loop": True, "request_id": "id", "untrusted": 1})
    result = json.loads(svc._ble_queue.get_nowait())
    assert result == {"cmd": "video", "action": "play", "path": "/videos/test.mjpg",
                      "fps": 12, "loop": True, "request_id": "id"}
    assert not replies and not errors


@pytest.mark.parametrize("update", [
    {"path": "/videos/../secret.mjpg"}, {"path": "/videos/a.mp4"},
    {"path": "/videos/sub/test.mjpg"}, {"path": "/videos/" + "a" * 100 + ".mjpg"},
    {"fps": 0}, {"fps": 21}, {"fps": True}, {"loop": "true"},
    {"request_id": ""}, {"action": "delete"},
])
def test_invalid_video_input_is_reported(update):
    svc, replies, errors = service()
    svc._on_video("", {"action": "play", "path": "/videos/a.mjpg", "request_id": "id", **update})
    assert replies[-1]["ok"] is False
    assert errors[-1] == replies[-1]
    assert svc._ble_queue.empty()


def test_video_queue_full_is_explicit_and_does_not_discard_existing_commands():
    svc, replies, errors = service()
    for _ in range(svc._ble_queue.maxsize):
        svc._ble_queue.put_nowait(b"existing")
    svc._on_video("", {"action": "stop", "request_id": "stop-id"})
    assert replies[-1]["error"] == "display_queue_full"
    assert errors[-1] == replies[-1]
    assert svc._ble_queue.get_nowait() == b"existing"


def test_status_queue_overflow_reports_evicted_video_request():
    svc, replies, errors = service()
    svc._on_video("", {"action": "stop", "request_id": "pending"})
    for _ in range(svc._ble_queue.maxsize - 1):
        svc._ble_queue.put_nowait(b'{"cmd":"mouth","state":"neutral"}\n')
    svc.send_command("status", state="ready")
    assert replies[-1]["request_id"] == "pending"
    assert replies[-1]["error"] == "display_queue_full"
    assert errors[-1] == replies[-1]


def test_fragmented_device_reply_and_async_error_are_surfaced():
    svc, replies, errors = service()
    data = (json.dumps({"cmd": "video", "ok": False, "error": "sd_card_missing",
                        "request_id": "one"}) + "\n").encode()
    for start in range(0, len(data), 7):
        svc._on_ble_notification(None, bytearray(data[start:start + 7]))
    assert replies[-1]["request_id"] == "one"
    assert errors[-1]["error"] == "sd_card_missing"
    svc._on_ble_notification(None, bytearray(b'{"event":"video_error","ok":false,"error":"corrupt"}\n'))
    assert errors[-1]["error"] == "corrupt"
    assert len(replies) == 1


def test_invalid_or_oversized_notification_is_reported():
    svc, replies, errors = service()
    svc._on_ble_notification(None, bytearray(b"not json\n[]\n"))
    assert [e["error"] for e in errors] == ["invalid_display_reply"] * 2
    svc._on_ble_notification(None, bytearray(b"a" * 8193))
    assert errors[-1]["error"] == "oversized_display_reply"
    assert not svc._notification_buffer


@pytest.mark.parametrize("ok", [False, True])
def test_cli_waits_for_correlated_device_result(monkeypatch, capsys, ok):
    script = Path(__file__).resolve().parents[1] / "scripts/desktop-assistant"
    namespace = runpy.run_path(str(script))
    func = namespace["cmd_display_video"]
    calls = []

    def request(message):
        calls.append(message)
        if message["cmd"] == "publish":
            return {"ok": True}
        request_id = calls[0]["payload"]["request_id"]
        assert message["topic"] == f"display.reply.{request_id}"
        return {"ok": True, "payload": {"cmd": "video", "ok": ok,
                                        "request_id": request_id, "error": "" if ok else "sd_missing"}}

    monkeypatch.setitem(func.__globals__, "_request", request)
    assert func(SimpleNamespace(video_action="status")) == (0 if ok else 1)
    assert json.loads(capsys.readouterr().out)["ok"] is ok


def test_cli_reports_device_reply_timeout(monkeypatch, capsys):
    namespace = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/desktop-assistant"))
    func = namespace["cmd_display_video"]
    times = iter((0, 0, 21))
    monkeypatch.setitem(func.__globals__, "time",
                        SimpleNamespace(monotonic=lambda: next(times), sleep=lambda _: None))
    monkeypatch.setitem(func.__globals__, "_request", lambda _: {"ok": True})
    assert func(SimpleNamespace(video_action="status")) == 1
    assert json.loads(capsys.readouterr().out)["error"] == "display_reply_timeout"


def test_cli_help_matches_registered_top_level_commands():
    path = Path(__file__).resolve().parents[1] / "scripts/desktop-assistant"
    tree = ast.parse(path.read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    help_func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cmd_help")
    commands = next(node.value for node in ast.walk(help_func)
                    if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "commands" for target in node.targets))
    registered = {node.args[0].value for node in ast.walk(main)
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                  and isinstance(node.func.value, ast.Name) and node.func.value.id == "sub"
                  and node.func.attr == "add_parser"}
    assert registered == {key.value for key in commands.keys}
