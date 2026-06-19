---
name: privacy
description: >
  Enable/disable VERA privacy mode and tune nudity-detection look-away behavior:
  rate, threshold, angle, cooldown, clear-frames, and announcement text.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Privacy Skill

Control VERA's privacy mode.

## When to use

- "Enable privacy mode"
- "Disable privacy mode"
- "What are your privacy settings?"
- "Set privacy threshold to 0.7"
- "Make privacy cooldown 5 seconds"

## Commands

| Command | Description |
|---|---|
| `status` | Show current privacy settings |
| `enable` | Enable nudity detection + look-away |
| `disable` | Disable privacy mode |
| `set key=value ...` | Update one or more tuning fields |

Supported `set` keys: `enabled`, `rate_hz`, `threshold`, `look_away_angle_deg`, `cooldown_s`, `clear_frames`, `announce`, `announce_text`, `resume_text`.

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/privacy/privacy.py <command> [args]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/privacy/privacy.py status
python3 ~/.openclaw/workspace/skills/privacy/privacy.py enable
python3 ~/.openclaw/workspace/skills/privacy/privacy.py set threshold=0.7 cooldown_s=5 clear_frames=4
```
