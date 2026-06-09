---
name: fan-control
description: >
  Query or set VERA fan thermal control points. Use for requests like
  "show fan curve", "set fan control points", "make fan ramp earlier",
  or "set fan curve to 25:0 47:80 50:100".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Fan Control Skill

Manage temperature-to-duty fan control points used by the thermal controller.

## When to use

- "Show the fan curve"
- "Set fan control points"
- "Make the fan ramp sooner"
- "Update fan curve to 25:0, 47:80, 50:100"

## Subcommands

| Subcommand | Effect |
|------------|--------|
| `status` | Return current control points as JSON (default) |
| `set <temp:duty>...` | Update control points and persist to config |

## How to invoke

```bash
# Get current fan curve
python3 ~/.openclaw/workspace/skills/fan-control/fan_control.py status

# Set a new fan curve
python3 ~/.openclaw/workspace/skills/fan-control/fan_control.py set 25:0 47:80 50:100
```

## Output

Returns JSON with:

- `ok` — operation result
- `control_points` — normalized list of `{temp_c, duty}` entries
- `runtime_applied` — true when thermal runtime was updated immediately
- `runtime_error` — included if thermal service was not reachable at runtime
