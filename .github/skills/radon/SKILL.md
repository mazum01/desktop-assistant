---
name: radon
description: >
  Read and announce the current basement radon level from the EcoQube radon
  monitor via the EcoSense cloud API. Returns the radon level in pCi/L and
  Bq/m³ with an EPA-based alert colour (Green / Orange / Red).
  Use for "what's the radon level?", "check radon", "how's the basement air?",
  "radon reading", "is the radon okay?", "announce radon level".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Radon Skill

Fetches the latest radon reading from the EcoQube in the basement via VERA's
cached EcoSense cloud data, then speaks the result aloud.

## When to use

- "What's the radon level?"
- "Check radon"
- "How's the basement air quality?"
- "Is the radon okay?"
- "Give me a radon reading"
- "Announce the radon level"
- "What does the radon monitor say?"

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/radon/radon.py
```

Optional flag — fetch without announcing (silent mode):
```bash
python3 ~/.openclaw/workspace/skills/radon/radon.py --silent
```

## Output

On success prints JSON:
```json
{
  "ok": true,
  "radon_pcil": 1.2,
  "radon_bqm3": 44.4,
  "alert": "Green",
  "device_name": "EcoQube - Basement",
  "last_updated": "2024-01-01T12:00:00+00:00"
}
```

### Alert levels (EPA thresholds)

| Level  | pCi/L         | Action                              |
|--------|---------------|-------------------------------------|
| Green  | < 2.7         | No action needed                    |
| Orange | 2.7 – 4.0     | EPA recommends considering mitigation |
| Red    | ≥ 4.0         | EPA recommends fixing your home     |

## Setup

Add EcoSense credentials to `/etc/desktop-assistant/secrets.env`:
```
ECOSENSE_USERNAME=your@email.com
ECOSENSE_PASSWORD=yourpassword
```

Then restart the daemon:
```bash
sudo systemctl restart desktop-assistant-core.service
```

## Notes

- The radon service polls EcoSense every 5 minutes and caches the result.
- This skill reads the cache — it returns instantly (no cloud round-trip).
- If the cache is empty (service just started), returns `{"available": false}`.
- If credentials are missing, returns `{"degraded": true}`.
- VERA also speaks an automatic TTS warning when the level exceeds 4.0 pCi/L
  (at most once per hour).

Summarise the reading naturally. Always mention the level in pCi/L and the
alert colour. If Orange or Red, mention the EPA recommendation.
Example: "The basement radon level is 1.2 picocuries per liter — that's Green,
well below the EPA action threshold."
