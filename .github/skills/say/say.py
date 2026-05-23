#!/usr/bin/env python3
"""Speak text aloud via VERA TTS system."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"ok": False, "error": "Usage: say.py <text to speak>"}))
        sys.exit(1)

    text = " ".join(sys.argv[1:])
    if not text.strip():
        print(json.dumps({"ok": False, "error": "Text cannot be empty"}))
        sys.exit(1)

    payload = json.dumps({"text": text}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}/api/say",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
            if result.get("ok"):
                print(json.dumps({"ok": True, "text": text, "message": f"Speaking: {text}"}))
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
