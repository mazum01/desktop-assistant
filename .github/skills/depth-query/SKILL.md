---
name: depth-query
description: >
  Query the depth/distance of objects and people in the camera's current view.
  Returns nearest, farthest, and mean distances, plus per-face depths for any
  recognized people. Use for "how far is the person?", "what's nearest?",
  "depth scan", "range report", "how close is everything?", "how far away is X?",
  "what is the distance to that object?", "scan the room".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Depth Query Skill

Ask VERA how far objects and people are from the camera.

## When to use

- "How far away is that?"
- "How close is the nearest object?"
- "What's the range to the person?"
- "Depth scan" / "Range report"
- "How far is [name]?"
- "What is the distance to the table?"
- "Scan the room for distances"

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/depth-query/depth_query.py
```

Prints JSON with fields:
- `ok` — true on success
- `nearest_m` — distance to nearest object in metres (null if unavailable)
- `farthest_m` — distance to farthest object in metres (null if unavailable)
- `mean_m` — mean scene depth in metres (null if unavailable)
- `valid_pct` — percentage of pixels with valid depth reading
- `calibrated` — whether stereo calibration is loaded
- `method` — "stereo", "face_size", or "unknown"
- `face_depths` — list of `{name, face_id, depth_m, method}` for visible people
- `ts` — unix timestamp of the depth reading

## Notes

- Dense depth map requires `depth.dense_enabled: true` in `config/assistant.yaml`
  and a stereo calibration file at `config/stereo_cal.npz`.
- Without calibration, face-size depth estimates are still returned for known faces.
- Run `python3 scripts/calibrate_stereo.py` to create the calibration file.
