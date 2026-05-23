---
name: system-status
description: >
  Get the health and telemetry status of VERA: CPU, memory,
  temperature, fan speed, servo angle, camera FPS, face count, and running
  services. Use for "how are you?", "system status", "what's your temperature?",
  "are you running okay?", and similar health queries.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# System Status Skill

Fetch a health and telemetry summary from VERA.

## When to use

- "How are you doing?" / "How are you feeling?"
- "System status" / "What's your status?"
- "What's your temperature?" / "Are you running hot?"
- "How's the CPU?" / "Memory usage?"
- "What's the camera FPS?"
- "Are all services running?"
- "Is everything okay?"

## How to invoke

No arguments needed.

```bash
python3 ~/.openclaw/workspace/skills/system-status/system_status.py
```

On success prints JSON with fields:
- `version` — firmware version string
- `cpu_percent` — CPU load (%)
- `mem_percent` — RAM usage (%)
- `temp_c` — CPU/board temperature (°C) if TMP117 available
- `fan_pct` — fan duty cycle (%)
- `servo_angle` — current camera pan angle (°)
- `cam1_fps` / `cam2_fps` — camera frame rates
- `faces_visible` — number of faces currently detected
- `services` — dict of service name → state

Summarize the key metrics for the user in natural language.
Highlight anything abnormal (high temp, stopped services, etc.).
