"""Updater controls use only authenticated, fixed local-socket operations."""
import asyncio
import ast
import importlib.machinery
import importlib.util
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.core.bus import MessageBus
from src.services.web_service import WebService
from src.updater import client as updater_client


@pytest.fixture
def update_client(monkeypatch):
    request = MagicMock(return_value={"accepted": True, "status": {"phase": "checking"}})
    monkeypatch.setattr(updater_client, "request", request)
    service = WebService(bus=MessageBus(), port=18080, api_key="test-key")
    with TestClient(service._build_app(), headers={"X-API-Key": "test-key"}) as client:
        yield client, request


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/update/status"),
    ("POST", "/api/update/check"),
    ("POST", "/api/update/install"),
    ("PUT", "/api/update/settings"),
])
def test_updates_require_configured_key(monkeypatch, method, path):
    request = MagicMock()
    monkeypatch.setattr(updater_client, "request", request)
    service = WebService(bus=MessageBus(), port=18080)
    with TestClient(service._build_app()) as client:
        assert client.request(method, path, json={}).status_code == 403
    request.assert_not_called()


@pytest.mark.parametrize("key", [None, "wrong"])
@pytest.mark.parametrize("method,path", [
    ("GET", "/api/update/status"),
    ("POST", "/api/update/check"),
    ("POST", "/api/update/install"),
    ("PUT", "/api/update/settings"),
])
def test_update_auth_middleware(update_client, key, method, path):
    client, request = update_client
    headers = {} if key is None else {"X-API-Key": key}
    client.headers.pop("X-API-Key", None)
    assert client.request(method, path, headers=headers, json={}).status_code == 401
    request.assert_not_called()


@pytest.mark.parametrize("method,path,payload,operation,forwarded", [
    ("GET", "status", None, "status", None),
    ("POST", "check", {}, "check", None),
    ("POST", "install", {}, "install", {"retry": False}),
    ("POST", "install", {"retry": True}, "install", {"retry": True}),
    ("PUT", "settings", {"enabled": True, "start": "23:00", "end": "05:30"},
     "settings", {"enabled": True, "start": "23:00", "end": "05:30"}),
])
def test_update_fixed_operations(update_client, method, path, payload, operation, forwarded):
    client, request = update_client
    response = client.request(method, "/api/update/" + path, json=payload)
    assert response.status_code == 200
    assert response.json() == request.return_value
    request.assert_called_once_with(operation, forwarded)


def test_update_status_preserves_progress(update_client):
    client, request = update_client
    request.return_value = {
        "installed_version": "5.0.0", "available_version": "5.1.0",
        "phase": "installing", "message": "Installing", "pending_reboot": True,
        "settings": {"enabled": True, "start": "23:00", "end": "05:30"},
        "history": [{"phase": "checking", "message": "Checked", "version": "5.1.0", "ts": 10}],
    }
    assert client.get("/api/update/status").json() == request.return_value


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "check", {"repo": "other"}),
    ("POST", "install", {"path": "/arbitrary"}),
    ("POST", "install", {"retry": "true"}),
    ("POST", "install", {"retry": 1}),
    ("PUT", "settings", {"enabled": 1}),
    ("PUT", "settings", {"enabled": "false"}),
    ("PUT", "settings", {"start": "24:00"}),
    ("PUT", "settings", {"end": "23:60"}),
    ("PUT", "settings", {"start": "7:00"}),
    ("PUT", "settings", {"start": "1١:00"}),
    ("PUT", "settings", {"start": None}),
    ("PUT", "settings", {"start": "12:00", "end": "12:00"}),
    ("PUT", "settings", {"command": "anything"}),
    ("PUT", "settings", {}),
    ("PUT", "settings", []),
    ("POST", "install", "anything"),
])
def test_invalid_update_payloads(update_client, method, path, payload):
    client, request = update_client
    assert client.request(method, "/api/update/" + path, json=payload).status_code == 400
    request.assert_not_called()


def test_invalid_update_json(update_client):
    client, request = update_client
    assert client.post("/api/update/install", content="{").status_code == 400
    request.assert_not_called()


def test_partial_window_rejects_equal_times(update_client):
    client, request = update_client
    request.return_value = {"enabled": True, "start": "23:00", "end": "05:00"}
    assert client.put("/api/update/settings", json={"start": "05:00"}).status_code == 400
    request.assert_called_once_with("settings", None)


def test_partial_window_settings(update_client):
    client, request = update_client
    request.return_value = {"enabled": True, "start": "23:00", "end": "05:00"}
    assert client.put("/api/update/settings", json={"end": "06:00"}).status_code == 200
    assert request.call_args_list[-1].args == (
        "settings", {"enabled": True, "start": "23:00", "end": "06:00"})


def test_partial_enabled_settings(update_client):
    client, request = update_client
    request.return_value = {"enabled": True, "start": "23:00", "end": "05:00"}
    assert client.put("/api/update/settings", json={"enabled": False}).status_code == 200
    assert request.call_args_list[-1].args == (
        "settings", {"enabled": False, "start": "23:00", "end": "05:00"})


@pytest.mark.parametrize("error", [OSError("socket down"), ValueError("bad response"), RuntimeError("failed")])
@pytest.mark.parametrize("method,path,payload", [
    ("GET", "status", None), ("POST", "check", {}),
    ("POST", "install", {"retry": True}), ("PUT", "settings", {"enabled": True}),
])
def test_updater_unavailable(update_client, error, method, path, payload):
    client, request = update_client
    request.side_effect = error
    response = client.request(method, "/api/update/" + path, json=payload)
    assert response.status_code == 503
    assert response.json()["detail"] == "Updater unavailable"


def test_updater_request_runs_off_event_loop(monkeypatch):
    threads = []

    def request(*args):
        threads.append(threading.get_ident())
        return {"phase": "idle"}

    monkeypatch.setattr(updater_client, "request", request)
    service = WebService(bus=MessageBus(), port=18080, api_key="test-key")
    endpoint = next(route.endpoint for route in service._build_app().routes
                    if getattr(route, "path", "") == "/api/update/status")
    event_loop_thread = threading.get_ident()
    assert asyncio.run(endpoint()) == {"phase": "idle"}
    assert threads[0] != event_loop_thread


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts" / "desktop-assistant"
    loader = importlib.machinery.SourceFileLoader("update_controls_cli", str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.mark.parametrize("args,operation,payload", [
    (["status"], "status", None),
    (["check"], "check", None),
    (["install"], "install", {"retry": False}),
    (["install", "--retry"], "install", {"retry": True}),
])
def test_cli_update(cli, monkeypatch, capsys, args, operation, payload):
    request = MagicMock(return_value={"accepted": True})
    monkeypatch.setattr(updater_client, "request", request)
    monkeypatch.setattr("sys.argv", ["vera", "update", *args])
    assert cli.main() == 0
    request.assert_called_once_with(operation, payload)
    assert "accepted" in capsys.readouterr().out


def test_cli_update_backend_failure(cli, monkeypatch, capsys):
    monkeypatch.setattr(updater_client, "request", MagicMock(side_effect=OSError("offline")))
    monkeypatch.setattr("sys.argv", ["vera", "update", "status"])
    assert cli.main() == 1
    assert "Updater unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("args", [
    ["install", "--repo", "other"], ["status", "--retry"], ["settings"],
])
def test_cli_rejects_passthrough(cli, monkeypatch, args):
    request = MagicMock()
    monkeypatch.setattr(updater_client, "request", request)
    monkeypatch.setattr("sys.argv", ["vera", "update", *args])
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    request.assert_not_called()


@pytest.mark.parametrize("args", [["help"], ["help", "update"]])
def test_cli_update_help(cli, monkeypatch, capsys, args):
    monkeypatch.setattr("sys.argv", ["vera", *args])
    assert cli.main() == 0
    output = capsys.readouterr().out
    assert "update" in output
    if len(args) > 1:
        assert "status" in output and "check" in output and "install" in output
        assert "--retry" in output


def test_cli_help_matches_top_level_parsers(cli):
    tree = ast.parse(Path(cli.__file__).read_text())
    main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    help_func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "cmd_help")
    commands = next(node.value for node in help_func.body
                    if isinstance(node, ast.Assign) and any(
                        isinstance(target, ast.Name) and target.id == "commands" for target in node.targets))
    registered = {
        node.args[0].value for node in ast.walk(main)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser" and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "sub"
    }
    assert registered == {key.value for key in commands.keys}
