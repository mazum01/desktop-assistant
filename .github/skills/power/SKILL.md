---
name: power
description: >
  Reboot or shut down VERA's Raspberry Pi. IMPORTANT: always
  ask the user for explicit confirmation in the conversation before invoking
  this skill. Use for "reboot", "restart the Pi", "shut down", "power off",
  "turn off the assistant".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Power Skill

Reboot or shut down the Raspberry Pi that runs VERA.

## ⚠️ Mandatory confirmation

**Always ask the user to confirm before running this skill.**
Say something like: "Are you sure you want to reboot/shut down the assistant?
This will interrupt all activity." Only invoke the script after the user says
yes.

## Commands

| Command    | Description                      |
|------------|----------------------------------|
| `reboot`   | Reboot the Raspberry Pi          |
| `shutdown` | Shut down the Raspberry Pi       |

## How to invoke

After the user confirms, use the `exec` tool to run:

```bash
python3 ~/.openclaw/workspace/skills/power/power.py <command>
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/power/power.py reboot
python3 ~/.openclaw/workspace/skills/power/power.py shutdown
```

On success prints JSON:
```json
{"ok": true, "action": "reboot", "message": "Rebooting system…"}
```

After running, inform the user that the Pi is rebooting/shutting down and the
assistant will be offline for about 30–60 seconds.
