---
name: pan-camera
description: >
  Pan the VERA camera head to a specific angle (0–270°).
  Use this when the user asks to look left, look right, center, or face
  a specific direction. The physical range is 0° (full left) to 270°
  (full right), with 135° as center.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Pan Camera Skill

Pan VERA's camera servo to a requested angle.

## When to use

Invoke this skill when the user:
- Asks to "look left", "look right", "look center", "face me", "center yourself"
- Requests a specific heading like "pan to 90 degrees"
- Asks the assistant to track or face a direction

## Angle reference

Servo limits are **dynamic** and configurable via the GUI or `da servo limits` CLI.
Always query live limits before mapping named directions:

```bash
da servo status
```

This outputs a line like `Travel limits : 90.0° – 270.0°`.

Map named directions proportionally within [min, max]:

| Direction   | Formula                        |
|-------------|--------------------------------|
| Full left   | min                            |
| Left        | min + (max - min) * 0.17       |
| Center      | (min + max) / 2                |
| Right       | min + (max - min) * 0.83       |
| Full right  | max                            |

Example with limits 90°–270°: center = (90 + 270) / 2 = **180°**.

The script enforces the live limits and rejects out-of-range angles.

## How to invoke

Use the `exec` tool to run:

```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py <angle>
```

For example, to center the camera:
```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py 135
```

The script exits 0 on success and prints JSON `{"ok": true, "angle": <n>, "message": "..."}`.
On failure it exits non-zero and prints `{"ok": false, "error": "..."}`.

After running, confirm the action to the user in natural language, e.g.
"I've panned the camera to 135° (center)."
