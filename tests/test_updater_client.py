"""Client protocol tests."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.updater.client import request


def test_client_request():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.return_value = b'{"accepted": true}\n'
    with patch("src.updater.client.socket.socket", return_value=connection):
        assert request("install", {"retry": False}) == {"accepted": True}
    data = json.loads(connection.sendall.call_args.args[0])
    assert data == {"operation": "install", "payload": {"retry": False}}


def test_client_surfaces_failure():
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.return_value = b'{"ok": false, "error": "busy"}\n'
    with patch("src.updater.client.socket.socket", return_value=connection):
        with pytest.raises(RuntimeError, match="busy"):
            request("check")
