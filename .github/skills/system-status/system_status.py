#!/usr/bin/env python3
"""Fetch and summarize VERA health and telemetry."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"


def main():
    try:
        with urllib.request.urlopen(f"{BASE_URL}/api/status", timeout=5) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)

    last = data.get("last", {})
    thermal_temp = last.get("thermal.temp") or {}
    thermal_fan  = last.get("thermal.fan")  or {}
    faces        = last.get("perception.faces") or {}
    objects      = last.get("perception.objects") or {}

    temp_c = thermal_temp.get("temp_c")
    fan_pct = thermal_fan.get("duty_pct")

    result = {
        "ok":           True,
        "version":      data.get("version", "?"),
        "cpu_percent":  data.get("cpu_percent"),
        "mem_percent":  data.get("mem_percent"),
        "cam1_fps":     data.get("cam1_fps"),
        "cam2_fps":     data.get("cam2_fps"),
        "servo_angle":  data.get("servo_angle"),
        "services":     data.get("services", {}),
        "faces_visible": faces.get("count", 0),
    }
    if temp_c is not None:
        result["temp_c"] = round(temp_c, 1)
    if fan_pct is not None:
        result["fan_pct"] = round(fan_pct, 1)

    detected_objs = objects.get("objects", [])
    if detected_objs:
        result["objects_detected"] = [o.get("label") for o in detected_objs if o.get("label")]

    print(json.dumps(result))


if __name__ == "__main__":
    main()
