#!/usr/bin/env python3
"""Toggle or query depth estimation on VERA."""

import json
import sys
import urllib.request
import urllib.error
from urllib.request import Request

BASE_URL = "http://localhost:8080"


def get_settings() -> dict:
    with urllib.request.urlopen(f"{BASE_URL}/api/settings/depth", timeout=5) as resp:
        return json.loads(resp.read())


def put_settings(payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = Request(f"{BASE_URL}/api/settings/depth", data=body,
                  headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main():
    args = sys.argv[1:]

    try:
        if not args or args[0] == "status":
            data = get_settings()
            data["summary"] = (
                f"Dense stereo: {'on' if data.get('dense_enabled') else 'off'}, "
                f"Mono depth: {'on' if data.get('mono_enabled') else 'off'}, "
                f"Calibrated: {data.get('calibrated', False)}"
            )
            print(json.dumps(data, indent=2))
            return

        if len(args) < 2 or args[1] not in ("on", "off"):
            print(json.dumps({"ok": False, "error": "Usage: depth_toggle.py [dense|mono] [on|off] | status"}))
            sys.exit(1)

        kind = args[0]
        enabled = args[1] == "on"

        if kind == "dense":
            data = put_settings({"dense_enabled": enabled})
        elif kind == "mono":
            data = put_settings({"mono_enabled": enabled})
        else:
            print(json.dumps({"ok": False, "error": f"Unknown type '{kind}'. Use 'dense' or 'mono'."}))
            sys.exit(1)

        # Fetch updated status and report
        status = get_settings()
        status["summary"] = (
            f"Dense stereo: {'on' if status.get('dense_enabled') else 'off'}, "
            f"Mono depth: {'on' if status.get('mono_enabled') else 'off'}"
        )
        print(json.dumps(status, indent=2))

    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
