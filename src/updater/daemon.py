"""Root-owned local control daemon with serialized persistent work."""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import socket
import struct
import threading
import time
from pathlib import Path

from src.updater.contract import settings
from src.updater.engine import Engine

log = logging.getLogger(__name__)
POLICY = Path("/etc/desktop-assistant/updater-policy.json")
STATE = Path("/var/lib/vera-updater")
SOCKET = Path("/run/vera-updater/control.sock")
MAX_REQUEST = 4096


class Controller:
    def __init__(self, engine: Engine):
        self.engine = engine
        self.lock = threading.RLock()
        self.busy = False

    def submit(self, action: str, payload: dict | None = None) -> dict:
        with self.lock:
            if self.busy:
                raise RuntimeError("An updater operation is already running")
            if self.engine.state.get("transaction") and action != "resume":
                raise RuntimeError("An interrupted transaction requires recovery or manual repair")
            self.busy = True
        threading.Thread(target=self.work, args=(action, payload or {}), daemon=True).start()
        return {"accepted": True, "message": "Update operation accepted; check status for the result"}

    def work(self, action: str, payload: dict) -> None:
        try:
            if action == "install":
                self.engine.install(retry=payload.get("retry", False))
            elif action == "check":
                self.engine.check()
            elif action == "scheduled":
                self.engine.scheduled()
            elif action == "resume":
                self.engine.resume()
            else:
                raise ValueError("Unknown internal updater operation")
        except Exception as exc:
            log.exception("Updater operation failed")
            with self.lock:
                self.engine.state["error"] = str(exc)
                if not self.engine.state.get("transaction") and self.engine.state["phase"] not in ("rolled_back", "success"):
                    self.engine.phase("blocked", str(exc))
                else:
                    self.engine.save()
        finally:
            with self.lock:
                self.busy = False

    def handle(self, message: dict) -> dict:
        if not isinstance(message, dict) or set(message) != {"operation", "payload"}:
            raise ValueError("Invalid updater request")
        operation = message["operation"]
        payload = message["payload"]
        if payload is not None and not isinstance(payload, dict):
            raise ValueError("Payload must be an object")
        if operation == "status":
            if payload:
                raise ValueError("Status takes no arguments")
            with self.lock:
                return {**self.engine.status(), "busy": self.busy}
        if operation == "settings":
            with self.lock:
                if payload is None:
                    return dict(self.engine.state["settings"])
                if self.busy:
                    raise RuntimeError("Wait for the current operation before changing settings")
                value = settings(payload)
                self.engine.state["settings"] = value
                self.engine.save()
                return {"ok": True, "settings": value}
        if operation in ("check", "scheduled"):
            if payload:
                raise ValueError("Check/schedule takes no arguments")
            return self.submit(operation)
        if operation == "install":
            if payload is not None and (set(payload) - {"retry"} or
                                        type(payload.get("retry", False)) is not bool):
                raise ValueError("Install accepts only boolean retry")
            return self.submit(operation, payload)
        raise ValueError("Unsupported updater operation")


def serve(policy: dict) -> None:
    engine = Engine(policy, STATE)
    controller = Controller(engine)
    STATE.mkdir(parents=True, mode=0o700, exist_ok=True)
    with (STATE / "daemon.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if engine.state.get("transaction") and engine.state["phase"] != "manual_repair":
            controller.submit("resume")
        SOCKET.parent.mkdir(parents=True, mode=0o750, exist_ok=True)
        os.chown(SOCKET.parent, 0, policy["gid"])
        SOCKET.unlink(missing_ok=True)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            server.bind(str(SOCKET))
            os.chown(SOCKET, 0, policy["gid"])
            SOCKET.chmod(0o660)
            server.listen(8)
            server.settimeout(30)
            while True:
                try:
                    connection, _ = server.accept()
                except socket.timeout:
                    continue
                with connection:
                    connection.settimeout(5)
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid not in (0, policy["uid"]):
                        connection.sendall(b'{"ok":false,"error":"Unauthorized local updater client"}\n')
                        continue
                    try:
                        data = bytearray()
                        while b"\n" not in data:
                            chunk = connection.recv(1024)
                            if not chunk:
                                raise ValueError("Incomplete updater request")
                            data.extend(chunk)
                            if len(data) > MAX_REQUEST:
                                raise ValueError("Updater request exceeds size limit")
                        result = controller.handle(json.loads(data.split(b"\n", 1)[0]))
                    except (ValueError, RuntimeError, OSError) as exc:
                        log.warning("Rejected updater request: %s", exc)
                        result = {"ok": False, "error": str(exc)}
                    try:
                        connection.sendall(json.dumps(result).encode() + b"\n")
                    except OSError:
                        log.warning("Updater client disconnected before receiving response")


def main() -> None:
    parser = argparse.ArgumentParser(description="VERA root-owned release updater")
    parser.add_argument("--scheduled", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if args.scheduled:
        from src.updater.client import request
        print(json.dumps(request("scheduled")))
        return
    if os.geteuid() != 0:
        raise SystemExit("The updater daemon must run as root via its installed systemd unit")
    policy = json.loads(POLICY.read_text())
    serve(policy)


if __name__ == "__main__":
    main()
