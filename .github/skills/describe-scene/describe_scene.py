#!/usr/bin/env python3
"""Trigger a spoken scene description from the desktop assistant camera."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def main():
    req = urllib.request.Request(
        f"{BASE_URL}/api/vision/describe",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(json.dumps({
                    "ok": True,
                    "message": "Vision description triggered — assistant is speaking"
                }))
            else:
                print(json.dumps({"ok": False, "error": str(result)}))
                sys.exit(1)
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
