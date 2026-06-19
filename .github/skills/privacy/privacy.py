#!/usr/bin/env python3
"""Control VERA privacy mode and tuning settings."""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"
COMMANDS = ("status", "enable", "disable", "set")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _put(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": (
                "Usage: privacy.py <command> [args]\n"
                "Commands: status, enable, disable, "
                "set key=value [key=value ...]"
            ),
        }))
        sys.exit(1)

    cmd = sys.argv[1]
    try:
        if cmd == "status":
            data = _get("/api/settings/privacy")
            data["ok"] = True
            print(json.dumps(data))
            return

        if cmd in ("enable", "disable"):
            enabled = cmd == "enable"
            _put("/api/settings/privacy", {"enabled": enabled})
            print(json.dumps({
                "ok": True,
                "enabled": enabled,
                "message": f"Privacy mode {'enabled' if enabled else 'disabled'}",
            }))
            return

        patch: dict[str, object] = {}
        for item in sys.argv[2:]:
            if "=" not in item:
                raise ValueError(f"Expected key=value, got: {item}")
            key, raw = item.split("=", 1)
            key = key.strip()
            raw = raw.strip()
            if key in ("enabled", "announce"):
                patch[key] = raw.lower() in ("1", "true", "on", "yes")
            elif key in ("clear_frames",):
                patch[key] = int(raw)
            elif key in ("rate_hz", "threshold", "look_away_angle_deg", "cooldown_s"):
                patch[key] = float(raw)
            elif key in ("announce_text", "resume_text"):
                patch[key] = raw
            else:
                raise ValueError(f"Unsupported key: {key}")
        if not patch:
            raise ValueError("set requires at least one key=value")
        _put("/api/settings/privacy", patch)
        print(json.dumps({"ok": True, "updated": patch}))
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
