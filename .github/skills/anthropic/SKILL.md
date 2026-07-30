---
name: anthropic
description: >
  Enable, disable, or check the status of the Anthropic Claude API on VERA.
  This is a global switch covering both direct usage (RoomService's
  camera-based room identification) and indirect usage (FaceService's
  OpenClaw-routed greeting generation, when configured to use a
  Claude/Anthropic model). Use for requests about turning on/off Claude,
  Anthropic, or cloud AI usage.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Anthropic API Toggle Skill

Control whether VERA is allowed to call the Anthropic Claude API.

## When to use

- "Enable the Anthropic API" / "Turn on Claude"
- "Disable the Anthropic API" / "Turn off Claude" / "Stop using Anthropic"
- "Is the Anthropic API on?" / "Anthropic API status"

## What this affects

- **RoomService**: skips the Claude vision-model call used to help identify
  which room VERA is in (falls back to visual-signature-only detection).
- **FaceService**: skips OpenClaw-routed greeting generation, but only when
  the configured OpenClaw model is Anthropic/Claude-branded (a non-Anthropic
  OpenClaw model, if configured, is unaffected).

## Commands

| Command   | Description                                  |
|-----------|-----------------------------------------------|
| `enable`  | Turn on Anthropic API usage                    |
| `disable` | Turn off Anthropic API usage                   |
| `status`  | Returns current enabled state                  |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/anthropic/anthropic.py <command>
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/anthropic/anthropic.py enable
python3 ~/.openclaw/workspace/skills/anthropic/anthropic.py disable
python3 ~/.openclaw/workspace/skills/anthropic/anthropic.py status
```

On success prints JSON:
```json
{"ok": true, "enabled": true, "message": "Anthropic API enabled"}
```
