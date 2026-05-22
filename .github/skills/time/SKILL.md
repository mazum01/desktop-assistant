---
name: time
description: >
  Ask the desktop assistant to announce the current time aloud via TTS.
  Use for "what time is it?", "tell me the time", "announce the time".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Time Skill

Asks the desktop assistant to speak the current time aloud via TTS.

## When to use

- "What time is it?"
- "Tell me the time"
- "Announce the time"
- "What's the current time?"

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/time/time.py
```

On success prints JSON:
```json
{"ok": true, "message": "Time announcement triggered"}
```
