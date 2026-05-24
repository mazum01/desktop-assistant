#!/usr/bin/env python3
"""Record microphone audio through the VERA web API."""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"


def _post(path: str, payload: dict, timeout: int = 60) -> dict:
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
    seconds = 5.0
    output = None

    if len(sys.argv) >= 2:
        try:
            seconds = float(sys.argv[1])
        except ValueError:
            print(json.dumps({"ok": False, "error": "seconds must be a number"}))
            sys.exit(1)
    if len(sys.argv) >= 3:
        output = sys.argv[2]

    payload = {"seconds": seconds}
    if output:
        payload["path"] = output

    try:
        timeout = max(30, int(seconds) + 30)
        result = _post("/api/audio/record", payload, timeout=timeout)
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
