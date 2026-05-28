#!/usr/bin/env python3
"""
Fetch and announce the current basement radon reading from VERA's EcoQube cache.

Usage:
    python3 radon.py           # fetch + speak aloud
    python3 radon.py --silent  # fetch only (no TTS announcement)
"""

import argparse
import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch basement radon reading")
    parser.add_argument("--silent", action="store_true",
                        help="Return JSON only; do not speak aloud")
    args = parser.parse_args()

    try:
        data = _get("/api/radon")
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)

    if not data.get("available", True):
        print(json.dumps({
            "ok": False,
            "available": False,
            "error": "Radon service degraded — credentials not configured.",
        }))
        sys.exit(1)

    reading = data.get("reading")
    if not reading:
        print(json.dumps({
            "ok": False,
            "available": False,
            "error": "No radon reading cached yet — service may still be polling.",
        }))
        sys.exit(1)

    result = {
        "ok": True,
        "radon_pcil": reading.get("radon_pcil"),
        "radon_bqm3": reading.get("radon_bqm3"),
        "alert": reading.get("alert", "Unknown"),
        "device_name": reading.get("device_name", "EcoQube"),
        "last_updated": reading.get("last_updated"),
    }
    if reading.get("error"):
        result["device_error"] = reading["error"]

    if not args.silent:
        try:
            _post("/api/radon/announce", {})
        except Exception as exc:
            result["announce_error"] = str(exc)

    print(json.dumps(result))


if __name__ == "__main__":
    main()
