---
name: pan-camera
description: >
  Control VERA pan and head tilt, inspect travel limits, and configure
  distinct PCA9685 channels. Use for look left/right/up/down, center,
  tilt head, servo channels, or pan camera. Current servos have 180-degree
  travel, centered at 90 degrees, limited to 70-110 on each axis.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Pan Camera Skill

Query the selected body's live status before controlling either axis. The new
body defaults to pan channel 1 and tilt channel 0; the original body uses pan
channel 15 and has no tilt. Channel assignments are persistent and configurable.

## When to use

Invoke this skill when the user:
- Asks to "look left", "look right", "look center", "face me", "center yourself"
- Requests a specific heading like "pan to 90 degrees"
- Asks the assistant to track or face a direction
- Asks to look up/down, tilt the head, or change servo channels

## Angle reference

Servo limits are **dynamic** and configurable via the GUI or `da servo limits` CLI.
Always query live limits before mapping named directions:

```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py --status
```

Read pan `soft_min_deg`/`soft_max_deg` and tilt
`tilt_min_deg`/`tilt_max_deg` from the returned `status` object.
Only request tilt when `tilt_available` is true.
Never assume limits if the query fails.

Map named directions proportionally within [min, max]:

| Direction   | Formula                        |
|-------------|--------------------------------|
| Full left   | min                            |
| Left        | min + (max - min) * 0.17       |
| Center      | (min + max) / 2                |
| Right       | min + (max - min) * 0.83       |
| Full right  | max                            |

With limits 70-110, center is **90 degrees**. Up/down use the tilt minimum/
maximum respectively (reverse if required by the mounting).

The script enforces the live limits and rejects out-of-range angles.

## How to invoke

Use the `exec` tool to run:

```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py <angle>
```

For example, to center the camera:
```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py 90
```

Tilt and persistent channel assignments:

```bash
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py --axis tilt 90
python3 ~/.openclaw/workspace/skills/pan-camera/pan_camera.py --pan-channel 1 --tilt-channel 0
```

The CLI provides the same controls:

```bash
da servo channels --pan 1 --tilt 0
da servo tilt --to 90
da servo limits --axis tilt --min 70 --max 110
da servo limits --axis pan --min 70 --max 110
```

Changing channels releases both old outputs and clears queued moves; it does
not immediately move the newly selected outputs. Do not reassign channels
while autonomous tracking is active. For a requested movement test, disable
tracking/random motion, announce and wait for speech completion before EACH
move, test each axis at center, center - 20, center + 20, then center, and
restore prior tracking settings. Abort on errors or limits that exclude a step.

The script exits 0 on success and prints JSON `{"ok": true, "angle": <n>, "message": "..."}`.
On failure it exits non-zero and prints `{"ok": false, "error": "..."}`.

After running, confirm the action to the user in natural language, e.g.
"I've requested pan to 90 degrees (center)."
