---
name: object-detection
description: >
  Enable, disable, or check the status of COCO object detection on the
  VERA (powered by the Hailo-8 AI accelerator). When enabled,
  objects in the camera view are classified and labeled. Use for requests
  about seeing/detecting objects or toggling AI vision.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Object Detection Skill

Control COCO object detection (Hailo-8 AI accelerator) on VERA.

## When to use

- "Enable object detection" / "Start detecting objects"
- "Disable object detection" / "Stop detecting objects" / "Turn off AI vision"
- "Is object detection on?" / "What objects can you see?"

## Commands

| Command   | Description                                          |
|-----------|------------------------------------------------------|
| `enable`  | Turn on Hailo-8 COCO object classification           |
| `disable` | Turn off object detection (reduces CPU/Hailo load)   |
| `status`  | Returns current enabled state + last detected objects|

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/object-detection/object_detection.py <command>
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/object-detection/object_detection.py enable
python3 ~/.openclaw/workspace/skills/object-detection/object_detection.py disable
python3 ~/.openclaw/workspace/skills/object-detection/object_detection.py status
```

On success prints JSON:
```json
{"ok": true, "enabled": true, "message": "Object detection enabled"}
```
For `status`, also includes `objects` list of recently detected items if available.
