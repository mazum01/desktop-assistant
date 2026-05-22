#!/usr/bin/env python3
"""Trigger the assistant to speak its current software version aloud."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def _post(path: str) -> dict:
    data = b"{}"
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main():
    try:
        _post("/api/version")
        print(json.dumps({"ok": True, "message": "Version announcement triggered"}))
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
