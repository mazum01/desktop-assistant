---
name: drop
description: >
  Query or announce the DROP water softener system status via local MQTT.
  Returns flow rate, daily usage, softener capacity, system pressure, TDS
  (total dissolved solids), salt level, and water supply state. Speaks the
  reading aloud via TTS on request. Use for "what's the water softener doing?",
  "how much water have we used today?", "is the salt low?", "check the water
  pressure", "DROP status", "water usage", "softener capacity remaining",
  "is there a water leak?", "announce water system status".
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# DROP Skill

Query the DROP water softener system connected to VERA via local MQTT.

## When to use

- "What's the water softener status?" / "How's the water system?"
- "How much water have we used today?"
- "Is the salt low?" / "Does the softener need salt?"
- "What's the water pressure?" / "Check the water pressure"
- "What's the TDS?" / "How hard is the water?"
- "Is the water on or off?" / "Is there a leak?"
- "Softener capacity remaining" / "When does the softener need to regenerate?"
- "Announce the water status" / "Tell me the DROP reading out loud"

## Subcommands

| Subcommand  | Effect |
|-------------|--------|
| `status`    | Return current reading as JSON (default if omitted) |
| `announce`  | Speak the reading aloud via TTS |

## How to invoke

```bash
# Get current reading (default)
python3 ~/.openclaw/workspace/skills/drop/drop.py

# Explicitly request status
python3 ~/.openclaw/workspace/skills/drop/drop.py status

# Speak the reading aloud
python3 ~/.openclaw/workspace/skills/drop/drop.py announce
```

## Output (status)

```json
{
  "available": true,
  "reading": {
    "softener_name": "Softener",
    "flow_gpm": 0.0,
    "used_today_gal": 52.3,
    "capacity_remaining_gal": 1450.0,
    "pressure_psi": 65.0,
    "pressure_high_psi": 72,
    "pressure_low_psi": 58,
    "tds_in_ppm": 320,
    "tds_out_ppm": 12,
    "salt_low": false,
    "water_on": true,
    "bypass_on": false,
    "protect_mode": "home",
    "last_updated": 1717000000.0
  },
  "devices": [
    {"key": "DROP-abc123_1", "name": "Softener", "type": "soft", "last_seen": 1717000000.0}
  ]
}
```

## Key fields

| Field | Description |
|-------|-------------|
| `flow_gpm` | Current water flow in gallons per minute |
| `used_today_gal` | Water used today in gallons |
| `capacity_remaining_gal` | Softener treatment capacity remaining (gal) |
| `pressure_psi` | Current system pressure |
| `tds_in_ppm` / `tds_out_ppm` | Water hardness in/out (PPM) |
| `salt_low` | True if brine tank salt is low |
| `water_on` | True = water supply open, False = shut off |
| `bypass_on` | True = softener bypassed |
| `protect_mode` | home / away / schedule / off |

## Response guidance

- For `status`: summarize the key metrics. Highlight any alerts (salt low, water off, leak).
  Example: "Your DROP softener has used 52 gallons today with 1450 gallons of capacity remaining.
  Pressure is 65 PSI. Salt level is okay."
- For `announce`: confirm to the user that the reading is being spoken aloud.
- If `available` is false, explain that the DROP Hub needs to be configured to connect to VERA's
  MQTT broker. The user should open the DROP app → System → Advanced → Configure MQTT and enter
  this Pi's IP address on port 1883.
