---
name: say
description: >
  Speak any text aloud via VERA's text-to-speech (TTS) system.
  Use when the user asks the assistant to "say", "announce", "speak", "read out",
  or "tell everyone" something.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Say Skill

Make VERA speak any text through its speakers.

## When to use

- "Say good morning"
- "Announce that dinner is ready"
- "Tell everyone the meeting starts in 5 minutes"
- "Read out this message: ..."
- "Speak the text: ..."

## Arguments

- `text` — the text to speak (required, can be quoted or unquoted multi-word)

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/say/say.py "Hello, good morning!"
```

On success prints JSON:
```json
{"ok": true, "text": "Hello, good morning!", "message": "Speaking: Hello, good morning!"}
```

After running, confirm to the user that the message is being spoken.
