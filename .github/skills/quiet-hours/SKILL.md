---
name: quiet-hours
description: >
  Enable, disable, or configure the quiet-hours TTS silence window on the
  desktop assistant. During quiet hours the assistant suppresses all spoken
  output and autonomous motion. Use for bedtime, sleep, or scheduled silence
  requests.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Quiet Hours Skill

Control the desktop assistant's quiet-hours (TTS + motion silence) window.

## When to use

- "Enable quiet hours" / "Go quiet" / "Shhh" / "Quiet mode on"
- "Disable quiet hours" / "You can talk again" / "Quiet mode off"
- "Are quiet hours on?" / "When is quiet time?"
- "Set quiet hours from 10pm to 7am"

## Commands

| Command              | Description                                      |
|----------------------|--------------------------------------------------|
| `enable`             | Turn on quiet hours immediately                  |
| `disable`            | Turn off quiet hours immediately                 |
| `status`             | Show current state, start time, and end time     |
| `set <start> <end>`  | Set the window (HH:MM 24h, e.g. `22:00 07:00`)  |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/quiet-hours/quiet_hours.py <command> [start] [end]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/quiet-hours/quiet_hours.py status
python3 ~/.openclaw/workspace/skills/quiet-hours/quiet_hours.py enable
python3 ~/.openclaw/workspace/skills/quiet-hours/quiet_hours.py disable
python3 ~/.openclaw/workspace/skills/quiet-hours/quiet_hours.py set 22:00 07:00
```

On success prints JSON:
```json
{"ok": true, "enabled": true, "start": "22:00", "end": "07:00", "message": "..."}
```
