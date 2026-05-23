#!/usr/bin/env python3
"""Pan the VERA camera to a given angle via ZMQ IPC."""

import json
import sys

REP_ENDPOINT = "ipc:///tmp/desktop-assistant.rep"


def get_servo_limits():
    """Query live servo limits from `da servo status`. Falls back to (0, 270)."""
    import subprocess, re
    try:
        out = subprocess.check_output(["da", "servo", "status"], stderr=subprocess.DEVNULL, text=True)
        m = re.search(r"Travel limits\s*:\s*([\d.]+)°\s*[–-]\s*([\d.]+)°", out)
        if m:
            return float(m.group(1)), float(m.group(2))
    except Exception:
        pass
    return 0.0, 270.0


def main():
    if len(sys.argv) != 2:
        print(json.dumps({"ok": False, "error": "Usage: pan_camera.py <angle_degrees>"}))
        sys.exit(1)

    try:
        angle = float(sys.argv[1])
    except ValueError:
        print(json.dumps({"ok": False, "error": f"Invalid angle: {sys.argv[1]!r}"}))
        sys.exit(1)

    min_angle, max_angle = get_servo_limits()
    if not (min_angle <= angle <= max_angle):
        print(json.dumps({
            "ok": False,
            "error": f"Angle {angle} out of range [{min_angle}, {max_angle}]"
        }))
        sys.exit(1)

    try:
        import zmq
    except ImportError:
        print(json.dumps({"ok": False, "error": "pyzmq not installed on this host"}))
        sys.exit(1)

    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.setsockopt(zmq.SNDTIMEO, 3000)
    try:
        sock.connect(REP_ENDPOINT)
        req = json.dumps({
            "cmd": "publish",
            "topic": "motion.pan_to",
            "payload": {"angle": angle, "override_quiet": True}
        })
        sock.send_string(req)
        raw = sock.recv_string()
        reply = json.loads(raw)
        if reply.get("ok"):
            print(json.dumps({"ok": True, "angle": angle, "message": f"Panning to {angle}°"}))
        else:
            print(json.dumps({"ok": False, "error": reply.get("error", "unknown")}))
            sys.exit(1)
    except zmq.Again:
        print(json.dumps({"ok": False, "error": "Timeout — VERA daemon not responding"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
    finally:
        sock.close()


if __name__ == "__main__":
    main()
