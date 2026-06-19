---
name: system-status
description: >
  Get the health and telemetry status of VERA: CPU, memory,
  temperature, fan speed, servo angle, camera FPS, face count, and running
  services. Use for "how are you?", "system status", "what's your temperature?",
  "are you running okay?", and similar health queries.
  Also supports listing active processes: vera-core, child processes (pw-record, etc.),
  and companion services (PipeWire, WirePlumber, pianobar).
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# System Status Skill

Fetch a health and telemetry summary from VERA, or list active processes.

## When to use

- "How are you doing?" / "How are you feeling?"
- "System status" / "What's your status?"
- "What's your temperature?" / "Are you running hot?"
- "How's the CPU?" / "Memory usage?"
- "What's the camera FPS?"
- "Are all services running?"
- "Is everything okay?"
- "What processes are running?" / "List active processes"

## Commands

| Command      | Description |
|---|---|
| `status`     | Health and telemetry snapshot (default) |
| `processes`  | List vera-core, child, and companion processes |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/system-status/system_status.py [command]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/system-status/system_status.py
python3 ~/.openclaw/workspace/skills/system-status/system_status.py status
python3 ~/.openclaw/workspace/skills/system-status/system_status.py processes
```

On `status` success prints JSON with fields:
- `version` — firmware version string
- `cpu_percent` — CPU load (%)
- `mem_percent` — RAM usage (%)
- `temp_c` — board temperature (°C) if TMP117 available
- `fan_pct` — fan duty cycle (%)
- `fan_rpm` — fan speed in RPM (omitted if tach disabled or no signal)
- `servo_angle` — current camera pan angle (°)
- `cam1_fps` / `cam2_fps` — camera frame rates
- `faces_visible` — number of faces currently detected
- `services` — dict of service name → state

On `processes` success prints JSON with:
- `processes` — list of `{name, pid, role, status, cpu_pct, mem_mb, threads?}`

Summarize the key metrics for the user in natural language.
Highlight anything abnormal (high temp, stopped services, etc.).
