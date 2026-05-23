---
name: describe-scene
description: >
  Ask VERA to look at what its camera sees and speak a
  natural-language description aloud. Use when the user asks "what do you see?",
  "describe your surroundings", "what's in front of you?", or similar.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Describe Scene Skill

Trigger VERA to capture its current camera view and speak
a natural-language description of what it sees.

## When to use

- "What do you see?"
- "Describe your surroundings"
- "What's in front of you?"
- "Look around and tell me what's there"
- "What's happening in the room?"

## How to invoke

No arguments — the assistant uses whatever it currently sees.

```bash
python3 ~/.openclaw/workspace/skills/describe-scene/describe_scene.py
```

On success prints JSON:
```json
{"ok": true, "message": "Vision description triggered — assistant is speaking"}
```

The description is spoken aloud via TTS. Confirm to the user that the
assistant is describing what it sees.
