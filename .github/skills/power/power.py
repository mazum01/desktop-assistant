#!/usr/bin/env python3
"""Reboot or shut down the Raspberry Pi."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"
COMMANDS = ("reboot", "shutdown")


def _post(path: str) -> dict:
    data = b"{}"
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else None
    if action not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: power.py <action>\nActions: {', '.join(COMMANDS)}"
        }))
        sys.exit(1)

    api_path = "/api/system/reboot" if action == "reboot" else "/api/system/shutdown"
    try:
        result = _post(api_path)
        msg = result.get("message", f"{action.capitalize()} initiated")
        print(json.dumps({"ok": True, "action": action, "message": msg}))
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()

