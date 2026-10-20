#!/usr/bin/env python3
"""Control VERA pan/tilt and channel assignments through local IPC."""

import argparse
import json
import math
import sys

REP_ENDPOINT = "ipc:///tmp/desktop-assistant.rep"


def request(message):
    import zmq

    sock = zmq.Context.instance().socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, 3000)
    sock.setsockopt(zmq.SNDTIMEO, 3000)
    try:
        sock.connect(REP_ENDPOINT)
        sock.send_json(message)
        reply = sock.recv_json()
        if not reply.get("ok"):
            raise RuntimeError(reply.get("error", "VERA request failed"))
        return reply
    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("angle", nargs="?", type=float)
    parser.add_argument("--axis", choices=("pan", "tilt"), default="pan")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--pan-channel", type=int)
    parser.add_argument("--tilt-channel", type=int)
    args = parser.parse_args()
    try:
        channels = (args.pan_channel, args.tilt_channel)
        if any(ch is not None for ch in channels):
            if (any(ch is None or not 0 <= ch <= 15 for ch in channels)
                    or args.pan_channel == args.tilt_channel):
                raise ValueError("Provide distinct --pan-channel and --tilt-channel (0-15)")
            reply = request({
                "cmd": "motion.set_channels",
                "pan_channel": args.pan_channel, "tilt_channel": args.tilt_channel,
            })
        else:
            reply = request({"cmd": "motion.get_status"})

        if args.angle is not None:
            status = reply["status"]
            mn = status["soft_min_deg" if args.axis == "pan" else "tilt_min_deg"]
            mx = status["soft_max_deg" if args.axis == "pan" else "tilt_max_deg"]
            if not math.isfinite(args.angle) or not mn <= args.angle <= mx:
                raise ValueError(f"{args.axis} angle must be within [{mn}, {mx}]")
            if not status["servo_enabled"] or not status["hardware_ready"]:
                raise RuntimeError("Servo motion is disabled or hardware is unavailable")
            reply = request({
                "cmd": "publish", "topic": f"motion.{args.axis}_to",
                "payload": {"angle": args.angle, "override_quiet": True},
            })
            reply.update(axis=args.axis, angle=args.angle, message="Movement requested")
        elif not args.status and all(ch is None for ch in channels):
            raise ValueError("Provide an angle, --status, or both channel assignments")
        print(json.dumps(reply))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
