"""Unprivileged client for the root-owned updater socket."""

from __future__ import annotations

import json
import socket

SOCKET = "/run/vera-updater/control.sock"
LIMIT = 262144


def request(operation: str, payload: dict | None = None) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(10)
        connection.connect(SOCKET)
        connection.sendall(json.dumps({"operation": operation, "payload": payload}).encode() + b"\n")
        chunks = bytearray()
        while b"\n" not in chunks:
            chunk = connection.recv(4096)
            if not chunk:
                raise RuntimeError("Updater disconnected without a result")
            chunks.extend(chunk)
            if len(chunks) > LIMIT:
                raise RuntimeError("Updater response is too large")
    result = json.loads(chunks.split(b"\n", 1)[0])
    if not isinstance(result, dict):
        raise RuntimeError("Invalid updater response")
    if result.get("error") and result.get("ok") is False:
        raise RuntimeError(result["error"])
    return result
