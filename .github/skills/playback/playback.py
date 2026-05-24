#!/usr/bin/env python3
"""Play back a recorded WAV clip through the VERA web API."""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"


def _post(path: str, payload: dict, timeout: int = 90) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def main() -> None:
    payload = {}
    if len(sys.argv) >= 2:
        payload["path"] = sys.argv[1]

    try:
        result = _post("/api/audio/playback", payload)
        if not result.get("ok"):
            print(json.dumps({"ok": False, "error": str(result)}))
            sys.exit(1)
        print(json.dumps(result))
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
