#!/usr/bin/env python3
"""Query or announce DROP water softener system status."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"

USAGE = "Usage: drop.py [status|announce]  (default: status)"


def cmd_status() -> dict:
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/drop", timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"Cannot reach assistant: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def cmd_announce() -> dict:
    req = urllib.request.Request(
        f"{BASE_URL}/api/drop/announce",
        data=b"",
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except urllib.error.URLError as exc:
        return {"ok": False, "error": f"Cannot reach assistant: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def main():
    subcommand = sys.argv[1].lower() if len(sys.argv) > 1 else "status"

    if subcommand in ("status", ""):
        result = cmd_status()
        if not result.get("available", True) and "degraded" in result:
            print(json.dumps({
                "ok": False,
                "available": False,
                "error": result.get("error", "DROP service degraded"),
            }))
            sys.exit(1)
    elif subcommand == "announce":
        result = cmd_announce()
    else:
        print(json.dumps({"ok": False, "error": f"Unknown subcommand '{subcommand}'. {USAGE}"}))
        sys.exit(1)

    print(json.dumps(result))
    if not result.get("ok", result.get("available", True)):
        sys.exit(1)


if __name__ == "__main__":
    main()
