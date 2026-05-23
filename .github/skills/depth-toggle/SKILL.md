---
name: depth-toggle
description: >
  Enable, disable, or check the status of depth estimation on VERA. Handles dense stereo SGBM depth (requires two cameras + calibration)
  and monocular Hailo neural depth. Use for "enable depth estimation",
  "turn on stereo depth", "disable mono depth", "turn off depth map",
  "enable depth scanning", "is depth estimation on?".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Depth Toggle Skill

Enable, disable, or query depth estimation on VERA.

## When to use

- "Enable depth estimation" / "Turn on depth scanning"
- "Turn on stereo depth" / "Enable dense depth"
- "Disable depth" / "Turn off the depth map"
- "Is depth estimation running?" / "Is depth on?"
- "Enable mono depth" / "Turn on Hailo depth"

## How to invoke

```bash
# Get current status
python3 ~/.openclaw/workspace/skills/depth-toggle/depth_toggle.py status

# Enable dense stereo depth
python3 ~/.openclaw/workspace/skills/depth-toggle/depth_toggle.py dense on

# Disable dense stereo depth
python3 ~/.openclaw/workspace/skills/depth-toggle/depth_toggle.py dense off

# Enable monocular Hailo depth (Phase 2)
python3 ~/.openclaw/workspace/skills/depth-toggle/depth_toggle.py mono on

# Disable monocular Hailo depth
python3 ~/.openclaw/workspace/skills/depth-toggle/depth_toggle.py mono off
```

Prints JSON with fields:
- `ok` — true on success
- `dense_enabled` — whether dense stereo depth is active
- `mono_enabled` — whether monocular neural depth is active
- `calibrated` — whether stereo calibration file is loaded

## Notes

- Dense depth requires two cameras and a stereo calibration file
  (`config/stereo_cal.npz`). Run `python3 scripts/calibrate_stereo.py` first.
- Monocular depth requires the Hailo-8 AI accelerator and `scdepthv3` model.
- Changes take effect immediately — no daemon restart needed.
