---
name: joke
description: >
  Tell a random dad joke aloud via the desktop assistant's text-to-speech
  system. Use for "tell me a joke", "say something funny", "make me laugh",
  "dad joke".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Joke Skill

Triggers the desktop assistant to speak a random dad joke aloud.

## When to use

- "Tell me a joke"
- "Say something funny"
- "Dad joke"
- "Make me laugh"

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/joke/joke.py
```

On success prints JSON:
```json
{"ok": true, "message": "Joke incoming!"}
```
