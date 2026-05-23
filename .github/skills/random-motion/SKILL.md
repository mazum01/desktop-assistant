---
name: random-motion
description: >
  Enable, disable, or check the status of random idle head movement on the
  VERA. When enabled, the head drifts and gazes around when no
  face is visible. Use for requests about idle head motion, wandering gaze,
  or keeping the head still.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Random Motion Skill

Control whether VERA's head drifts and gazes randomly when
idle (no face in view).

## When to use

- "Start moving your head randomly" / "Enable random motion"
- "Stop moving your head" / "Disable random motion" / "Stay still"
- "Is random motion on?" / "Are you moving randomly?"

## Commands

| Command   | Description                                      |
|-----------|--------------------------------------------------|
| `enable`  | Head drifts randomly when no face is visible     |
| `disable` | Head stays still when no face is visible         |
| `status`  | Returns current enabled/disabled state           |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/random-motion/random_motion.py <command>
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/random-motion/random_motion.py enable
python3 ~/.openclaw/workspace/skills/random-motion/random_motion.py disable
python3 ~/.openclaw/workspace/skills/random-motion/random_motion.py status
```

On success prints JSON:
```json
{"ok": true, "enabled": true, "message": "Random motion enabled"}
```
