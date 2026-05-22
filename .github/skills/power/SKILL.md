---
name: power
description: >
  Reboot or shut down the desktop assistant's Raspberry Pi. Requires a
  confirmation step before executing. Use for "reboot", "restart the Pi",
  "shut down", "power off", "turn off the assistant".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Power Skill

Reboot or shut down the Raspberry Pi that runs the desktop assistant.

**A confirmation prompt is always required before any power action is taken.**

## When to use

- "Reboot the Pi" / "Restart the system"
- "Shut down" / "Power off" / "Turn off"

## Commands

| Command    | Description                                 |
|------------|---------------------------------------------|
| `reboot`   | Reboot the Raspberry Pi (prompts to confirm)|
| `shutdown` | Shut down the Raspberry Pi (prompts to confirm)|

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/power/power.py <command> --confirm
```

The `--confirm` flag is required. Without it the skill prints instructions and
exits without doing anything, giving the AI agent a chance to confirm with the
user first.

Examples:
```bash
python3 ~/.openclaw/workspace/skills/power/power.py reboot --confirm
python3 ~/.openclaw/workspace/skills/power/power.py shutdown --confirm
```

**AI agent workflow:**
1. User asks to reboot/shutdown.
2. Agent calls the skill **without** `--confirm` first — skill returns `{"ok": false, "needs_confirm": true, "message": "..."}`.
3. Agent asks the user: "Are you sure you want to reboot/shutdown the assistant?"
4. Only if user confirms, agent calls skill again **with** `--confirm`.

On success prints JSON:
```json
{"ok": true, "action": "reboot", "message": "Rebooting system…"}
```
