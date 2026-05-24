---
name: playback
description: >
  Play back a previously recorded WAV clip through VERA's speakers.
  Use for requests like "play that recording", "play latest clip", or
  "play back this audio file".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Playback Skill

Play the latest recorded clip, or a specified WAV file.

## When to use

- "Play back the recording"
- "Play the latest clip"
- "Playback this WAV file"

## Arguments

- `input` (optional) - path to a WAV file; if omitted, uses latest recording

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/playback/playback.py [input_path]
```

Examples:

```bash
python3 ~/.openclaw/workspace/skills/playback/playback.py
python3 ~/.openclaw/workspace/skills/playback/playback.py /tmp/my_clip.wav
```

On success prints JSON like:

```json
{"ok": true, "path": "/home/starter/Pictures/vera/recordings/recording_20260524_103000.wav", "seconds": 5.0}
```

Confirm to the user that playback started/completed.
