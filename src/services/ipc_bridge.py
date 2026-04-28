"""
IPC bridge.

Exposes the in-process `MessageBus` to outside processes via ZeroMQ:

    PUB socket  →  forwards every bus event to subscribers
                   (topic, json-payload) two-frame messages.
    REP socket  →  receives JSON command requests; publishes them onto
                   the in-process bus and returns an ack.

This is the "expose IPC (DBus or ZeroMQ)" exit criterion for Phase 2.
External tools (CLI, dashboards, other Python processes) can monitor
telemetry and inject commands without sharing memory with the assistant.

ZeroMQ is a soft dependency. If `pyzmq` isn't installed the bridge
logs a warning at startup, marks itself as not running, and the rest
of the assistant continues normally.

Wire format (PUB):
    frame 0: topic (utf-8)
    frame 1: json-encoded payload (utf-8)

Wire format (REP request):
    {"cmd": "publish", "topic": "...", "payload": {...}}
    → reply: {"ok": true}

    {"cmd": "last", "topic": "..."}
    → reply: {"ok": true, "payload": <last payload>}

    {"cmd": "topics"}
    → reply: {"ok": true, "topics": [...]}

Endpoints (defaults):
    pub: ipc:///tmp/desktop-assistant.pub
    rep: ipc:///tmp/desktop-assistant.rep
Override via constructor args or environment:
    DA_BUS_PUB_ENDPOINT, DA_BUS_REP_ENDPOINT
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

try:
    import zmq
    _ZMQ_AVAILABLE = True
except ImportError:
    zmq = None  # type: ignore
    _ZMQ_AVAILABLE = False


_DEFAULT_PUB = "ipc:///tmp/desktop-assistant.pub"
_DEFAULT_REP = "ipc:///tmp/desktop-assistant.rep"


def _safe_dumps(obj) -> str:
    """JSON-encode *obj*, falling back to repr() for non-serialisable values."""
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return json.dumps({"_repr": repr(obj)})


class IPCBridge(Service):
    name = "ipc_bridge"
    tick_seconds = 1.0   # heartbeat only; real work is in the REP thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        pub_endpoint: Optional[str] = None,
        rep_endpoint: Optional[str] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._pub_endpoint = pub_endpoint or os.environ.get(
            "DA_BUS_PUB_ENDPOINT", _DEFAULT_PUB
        )
        self._rep_endpoint = rep_endpoint or os.environ.get(
            "DA_BUS_REP_ENDPOINT", _DEFAULT_REP
        )
        self._ctx = None
        self._pub = None
        self._rep = None
        self._rep_thread: Optional[threading.Thread] = None
        self._rep_stop = threading.Event()
        self._unsub = None
        self._enabled = False
        self._pub_lock = threading.Lock()

    def on_start(self) -> None:
        if not _ZMQ_AVAILABLE:
            log.warning(
                "pyzmq not installed — IPC bridge disabled "
                "(install with: sudo apt-get install -y python3-zmq)"
            )
            return

        try:
            self._ctx = zmq.Context.instance()
            self._pub = self._ctx.socket(zmq.PUB)
            self._pub.bind(self._pub_endpoint)
            self._rep = self._ctx.socket(zmq.REP)
            self._rep.bind(self._rep_endpoint)
        except Exception:
            log.exception("ZMQ socket bind failed — IPC bridge disabled")
            self._teardown_sockets()
            return

        self._unsub = self.bus.subscribe("*", self._forward_to_pub)

        self._rep_stop.clear()
        self._rep_thread = threading.Thread(
            target=self._rep_loop, name="ipc-rep", daemon=True
        )
        self._rep_thread.start()
        self._enabled = True
        log.info(
            "IPCBridge running; pub=%s rep=%s",
            self._pub_endpoint, self._rep_endpoint,
        )

    def on_stop(self) -> None:
        if self._unsub is not None:
            try:
                self._unsub()
            except Exception:
                pass
            self._unsub = None
        self._rep_stop.set()
        if self._rep_thread is not None:
            self._rep_thread.join(timeout=2.0)
            self._rep_thread = None
        self._teardown_sockets()
        self._enabled = False
        log.info("IPCBridge stopped")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ── Internals ──────────────────────────────────────────────────────

    def _teardown_sockets(self) -> None:
        for sock in (self._pub, self._rep):
            if sock is not None:
                try:
                    sock.close(linger=0)
                except Exception:
                    pass
        self._pub = None
        self._rep = None

    def _forward_to_pub(self, topic: str, payload) -> None:
        if self._pub is None:
            return
        try:
            with self._pub_lock:
                self._pub.send_multipart(
                    [topic.encode("utf-8"), _safe_dumps(payload).encode("utf-8")],
                    flags=zmq.NOBLOCK,
                )
        except Exception:
            log.debug("PUB forward failed for topic=%s", topic, exc_info=True)

    def _rep_loop(self) -> None:
        poller = zmq.Poller()
        poller.register(self._rep, zmq.POLLIN)
        while not self._rep_stop.is_set():
            try:
                events = dict(poller.poll(timeout=200))
            except Exception:
                if self._rep_stop.is_set():
                    break
                log.exception("REP poll error")
                continue
            if self._rep not in events:
                continue
            try:
                raw = self._rep.recv(flags=zmq.NOBLOCK)
            except Exception:
                continue
            reply = self._handle_request(raw)
            try:
                self._rep.send_string(_safe_dumps(reply))
            except Exception:
                log.exception("REP send failed")

    def _handle_request(self, raw: bytes) -> dict:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return {"ok": False, "error": "bad_json"}

        cmd = msg.get("cmd")
        if cmd == "publish":
            topic = msg.get("topic")
            if not isinstance(topic, str) or not topic:
                return {"ok": False, "error": "missing_topic"}
            self.bus.publish(topic, msg.get("payload"))
            return {"ok": True}
        if cmd == "last":
            topic = msg.get("topic")
            if not isinstance(topic, str) or not topic:
                return {"ok": False, "error": "missing_topic"}
            return {"ok": True, "payload": self.bus.last(topic)}
        if cmd == "topics":
            return {"ok": True, "topics": self.bus.topics()}
        if cmd == "ping":
            return {"ok": True, "pong": True}
        return {"ok": False, "error": f"unknown_cmd:{cmd}"}
