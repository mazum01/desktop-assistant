#!/usr/bin/env python3
"""Query or set VERA fan control points."""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"
USAGE = "Usage: fan_control.py [status|set <temp:duty> ...]"


def get_control_points() -> dict:
    with urllib.request.urlopen(f"{BASE_URL}/api/settings/fan/control-points", timeout=5) as resp:
        return json.loads(resp.read())


def set_control_points(specs: list[str]) -> dict:
    points = []
    for spec in specs:
        if ":" not in spec:
            raise ValueError(f"Invalid point '{spec}'. Use temp:duty")
        temp_s, duty_s = spec.split(":", 1)
        points.append({"temp_c": float(temp_s), "duty": float(duty_s)})
    if len(points) < 2:
        raise ValueError("At least two control points are required")

    req = urllib.request.Request(
        f"{BASE_URL}/api/settings/fan/control-points",
        data=json.dumps({"points": points}).encode(),
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main() -> None:
    args = sys.argv[1:]
    sub = args[0].lower() if args else "status"

    try:
        if sub in ("status", ""):
            result = get_control_points()
        elif sub == "set":
            result = set_control_points(args[1:])
        else:
            print(json.dumps({"ok": False, "error": f"Unknown subcommand '{sub}'. {USAGE}"}))
            sys.exit(1)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)

    print(json.dumps(result))
    if not result.get("ok", False):
        sys.exit(1)


if __name__ == "__main__":
    main()
