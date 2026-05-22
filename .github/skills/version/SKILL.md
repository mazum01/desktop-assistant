---
name: version
description: >
  Ask the desktop assistant to speak its current software version number aloud.
  Use for "what version are you?", "tell me your version", "what software are
  you running?".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Version Skill

Asks the desktop assistant to speak its current software version number via TTS.

## When to use

- "What version are you running?"
- "Tell me your version number"
- "Speak your version"
- "What software version is this?"

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/version/version.py
```

On success prints JSON:
```json
{"ok": true, "message": "Version announcement triggered"}
```
