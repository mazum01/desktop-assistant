#!/usr/bin/env python3
"""Enable, disable, configure, or check quiet-hours TTS silence window."""

import json
import sys
import urllib.request
import urllib.error
import re

BASE_URL = "http://localhost:8080"
COMMANDS = ("enable", "disable", "status", "set")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _put(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _valid_time(s: str) -> bool:
    return bool(re.match(r"^\d{2}:\d{2}$", s))


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: quiet_hours.py <command> [start end]\nCommands: {', '.join(COMMANDS)}"
        }))
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        current = _get("/api/settings/quiet-hours")

        if cmd == "status":
            enabled = current.get("enabled", False)
            start   = current.get("start", "21:00")
            end     = current.get("end",   "06:00")
            state   = "enabled" if enabled else "disabled"
            print(json.dumps({
                "ok": True, "enabled": enabled,
                "start": start, "end": end,
                "message": f"Quiet hours {state} ({start}–{end})"
            }))

        elif cmd in ("enable", "disable"):
            enabled = cmd == "enable"
            body = {
                "enabled": enabled,
                "start": current.get("start", "21:00"),
                "end":   current.get("end",   "06:00"),
            }
            result = _put("/api/settings/quiet-hours", body)
            state = "enabled" if enabled else "disabled"
            print(json.dumps({
                "ok": True, "enabled": enabled,
                "start": result.get("start", body["start"]),
                "end":   result.get("end",   body["end"]),
                "message": f"Quiet hours {state}"
            }))

        elif cmd == "set":
            if len(sys.argv) < 4:
                print(json.dumps({"ok": False,
                                  "error": "Usage: quiet_hours.py set <HH:MM> <HH:MM>"}))
                sys.exit(1)
            start, end = sys.argv[2], sys.argv[3]
            if not _valid_time(start) or not _valid_time(end):
                print(json.dumps({"ok": False,
                                  "error": "Times must be HH:MM format, e.g. 22:00"}))
                sys.exit(1)
            body = {"enabled": current.get("enabled", False), "start": start, "end": end}
            result = _put("/api/settings/quiet-hours", body)
            print(json.dumps({
                "ok": True,
                "enabled": result.get("enabled", body["enabled"]),
                "start": start, "end": end,
                "message": f"Quiet hours window set to {start}–{end}"
            }))

    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
