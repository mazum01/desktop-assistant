---
name: face-tracking
description: >
  Enable, disable, or check the status of face-following behavior on the
  desktop assistant's camera servo. When enabled, the head automatically
  follows detected faces. Use for requests about the head tracking, following,
  or staying still.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Face Tracking Skill

Control whether the desktop assistant's head servo automatically follows
detected faces.

## When to use

- "Follow me" / "Track faces" / "Enable face tracking"
- "Stop following me" / "Hold still" / "Disable face tracking"
- "Are you following faces?" / "Is face tracking on?"

## Commands

| Command   | Description                              |
|-----------|------------------------------------------|
| `enable`  | Head servo follows detected faces        |
| `disable` | Head servo stays put (manual pan only)   |
| `status`  | Returns current enabled/disabled state   |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/face-tracking/face_tracking.py <command>
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/face-tracking/face_tracking.py enable
python3 ~/.openclaw/workspace/skills/face-tracking/face_tracking.py disable
python3 ~/.openclaw/workspace/skills/face-tracking/face_tracking.py status
```

On success prints JSON:
```json
{"ok": true, "enabled": true, "message": "Face tracking enabled"}
```
