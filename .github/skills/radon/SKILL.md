---
name: radon
description: >
  Read and publish data from the EcoQube radon monitor in the basement.
  Returns current radon level in pCi/L, alert status (Green / Orange / Red),
  and device name. Can also speak the reading aloud via TTS. Use for
  "what's the radon level?", "is the radon okay?", "announce the radon reading",
  "radon status", "how's the air quality in the basement?".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Radon Skill

Query the EcoQube basement radon monitor or announce the current reading aloud.

## When to use

- "What's the radon level?" / "How's the radon?"
- "Is the basement radon safe?"
- "Radon status" / "Check the radon monitor"
- "How's the air quality in the basement?"
- "Announce the radon reading" / "Tell me the radon level out loud"
- "What does the EcoQube say?"

## Subcommands

| Subcommand  | Effect |
|-------------|--------|
| `status`    | Return current reading as JSON (default if omitted) |
| `announce`  | Speak the reading aloud via TTS |

## How to invoke

```bash
# Get current reading (default)
python3 ~/.openclaw/workspace/skills/radon/radon.py

# Explicitly request status
python3 ~/.openclaw/workspace/skills/radon/radon.py status

# Speak the reading aloud
python3 ~/.openclaw/workspace/skills/radon/radon.py announce
```

## Output (status)

```json
{
  "ok": true,
  "radon_pcil": 0.81,
  "radon_bqm3": 29.97,
  "alert": "Green",
  "device_name": "Radon Detect",
  "last_updated": 1717000000.0
}
```

## Alert levels

| Alert  | pCi/L range         | Guidance |
|--------|---------------------|----------|
| Green  | < 2.7               | Safe — well below EPA action threshold |
| Orange | 2.7 – 4.0           | Consider mitigation |
| Red    | ≥ 4.0               | EPA recommends fixing your home |

## Response guidance

- For `status`: report the pCi/L value and the alert colour in plain language.
  Example: "The basement radon is 0.81 picocuries per liter — that's Green, well below the EPA limit."
- For `announce`: confirm to the user that the reading is being spoken aloud.
- If `ok` is false, relay the error message.
