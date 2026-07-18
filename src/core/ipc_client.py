"""
IPCClient — shared ZeroMQ REQ-socket client for talking to any node's
IPCBridge (thermal, core, or any future `ProcessNode`).

Today `scripts/desktop-assistant` hand-rolls this exact REQ/timeout/LINGER
boilerplate twice (once for the core bridge, once for the thermal bridge:
`_request()` / `_thermal_request()`). Every additional process split adds
another copy-pasted variant. This module gives new code (and, eventually,
the CLI) one implementation to share.

Usage::

    client = IPCClient("ipc:///tmp/desktop-assistant-media.rep")
    reply = client.call({"cmd": "ping"})
    # -> {"ok": True, "pong": True}
"""

from __future__ import annotations

import json
from typing import Optional


class IPCClient:
    """Minimal synchronous REQ client for an `IPCBridge` REP endpoint."""

    def __init__(self, rep_endpoint: str, timeout_ms: int = 2000) -> None:
        self._rep_endpoint = rep_endpoint
        self._timeout_ms = timeout_ms

    def call(self, request: dict, timeout_ms: Optional[int] = None) -> dict:
        """Send *request* (must include a ``cmd`` key) and return the reply.

        Never raises for a timeout or connection error — returns
        ``{"ok": False, "error": "..."}`` instead, matching the existing
        CLI helper behavior so callers don't need special-case handling.
        """
        try:
            import zmq
        except ImportError:
            return {"ok": False, "error": "pyzmq not installed"}

        ms = timeout_ms if timeout_ms is not None else self._timeout_ms
        ctx = zmq.Context.instance()
        sock = ctx.socket(zmq.REQ)
        sock.setsockopt(zmq.LINGER, 0)
        sock.setsockopt(zmq.RCVTIMEO, ms)
        sock.setsockopt(zmq.SNDTIMEO, ms)
        try:
            sock.connect(self._rep_endpoint)
            sock.send_string(json.dumps(request))
            return json.loads(sock.recv_string())
        except zmq.error.Again:
            return {
                "ok": False,
                "error": f"timeout — is the node at {self._rep_endpoint} running?",
            }
        except Exception as exc:  # pragma: no cover - defensive
            return {"ok": False, "error": str(exc)}
        finally:
            sock.close(linger=0)

    def ping(self, timeout_ms: int = 500) -> bool:
        return bool(self.call({"cmd": "ping"}, timeout_ms=timeout_ms).get("ok"))
