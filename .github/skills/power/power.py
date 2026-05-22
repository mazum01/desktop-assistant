#!/usr/bin/env python3
"""Reboot or shut down the Raspberry Pi with a mandatory confirmation flag."""

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
    args = sys.argv[1:]
    confirmed = "--confirm" in args
    action = next((a for a in args if a in COMMANDS), None)

    if action is None:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: power.py <action> [--confirm]\nActions: {', '.join(COMMANDS)}"
        }))
        sys.exit(1)

    if not confirmed:
        verb = "reboot" if action == "reboot" else "shut down"
        print(json.dumps({
            "ok": False,
            "needs_confirm": True,
            "message": (
                f"This will {verb} the Raspberry Pi. "
                f"Please confirm by calling: power.py {action} --confirm"
            ),
        }))
        sys.exit(0)

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
