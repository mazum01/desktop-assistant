#!/usr/bin/env python3
"""Enable, disable, or check idle random head movement."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"
COMMANDS = ("enable", "disable", "status")


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


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: random_motion.py <command>\nCommands: {', '.join(COMMANDS)}"
        }))
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "status":
            data = _get("/api/settings/random-motion")
            enabled = data.get("enabled", False)
            state = "enabled" if enabled else "disabled"
            print(json.dumps({"ok": True, "enabled": enabled,
                              "message": f"Random motion is {state}"}))
        else:
            enabled = cmd == "enable"
            _put("/api/settings/random-motion", {"enabled": enabled})
            state = "enabled" if enabled else "disabled"
            print(json.dumps({"ok": True, "enabled": enabled,
                              "message": f"Random motion {state}"}))
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
