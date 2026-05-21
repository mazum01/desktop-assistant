#!/usr/bin/env python3
"""Query depth/distance of objects in the current camera view."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def main():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/depth/query", timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)

    if not data.get("ok"):
        print(json.dumps(data))
        sys.exit(1)

    # Build a human-readable summary
    lines = []
    nearest = data.get("nearest_m")
    farthest = data.get("farthest_m")
    mean = data.get("mean_m")
    method = data.get("method", "unknown")
    calibrated = data.get("calibrated", False)
    face_depths = data.get("face_depths", [])

    if nearest is not None:
        lines.append(f"Nearest object: {nearest:.2f} m")
    if farthest is not None:
        lines.append(f"Farthest object: {farthest:.2f} m")
    if mean is not None:
        lines.append(f"Mean depth: {mean:.2f} m")

    for f in face_depths:
        name = f.get("name") or "Unknown"
        d = f.get("depth_m")
        if d is not None:
            lines.append(f"{name}: {d:.2f} m away")

    if not lines:
        lines.append("No depth data available.")

    status_note = f"method={method}"
    if not calibrated:
        status_note += ", uncalibrated"
    lines.append(f"({status_note})")

    data["summary"] = " | ".join(lines)
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
