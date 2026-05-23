---
name: grab-frame
description: >
  Take a full-resolution still photo from a VERA camera.
  Use when the user asks to take a photo, snapshot, picture, or frame grab
  from the camera. Camera 1 is the detection/tracking camera; camera 2 is
  the secondary view. Saves the JPEG to ~/Pictures/vera/.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Grab Frame Skill

Capture a full-resolution JPEG still from camera 1 or camera 2.

## When to use

Invoke this skill when the user:
- Asks to "take a photo", "take a picture", "snapshot", "grab a frame"
- Wants to capture what the camera currently sees
- Asks to save or show what the assistant is looking at

## Arguments

- `camera` — `1` (detection camera, default) or `2` (secondary camera)

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/grab-frame/grab_frame.py [camera]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/grab-frame/grab_frame.py 1
python3 ~/.openclaw/workspace/skills/grab-frame/grab_frame.py 2
```

On success prints JSON:
```json
{"ok": true, "camera": 1, "path": "/home/starter/Pictures/vera/cam1_20260517_093000.jpg", "size_kb": 210, "message": "Saved 210 KB to ..."}
```

After running, tell the user the path where the image was saved.
If they want to view it, they can open it with their file manager or image viewer.
