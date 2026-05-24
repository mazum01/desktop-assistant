---
name: record
description: >
  Record microphone input to a WAV file on VERA.
  Use for requests like "record a clip", "start recording", "capture audio",
  or "record 10 seconds".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Record Skill

Record a microphone clip and save it as a WAV file.

## When to use

- "Record audio"
- "Record a 5 second clip"
- "Capture my voice"
- "Start a microphone recording"

## Arguments

- `seconds` (optional) - recording duration in seconds (default: 5, max: 120)
- `output` (optional) - explicit output WAV path

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/record/record.py [seconds] [output_path]
```

Examples:

```bash
python3 ~/.openclaw/workspace/skills/record/record.py
python3 ~/.openclaw/workspace/skills/record/record.py 10
python3 ~/.openclaw/workspace/skills/record/record.py 8 /tmp/my_clip.wav
```

On success prints JSON like:

```json
{"ok": true, "path": "/home/starter/Pictures/vera/recordings/recording_20260524_103000.wav", "seconds": 5.0}
```

Tell the user where the recording was saved.
