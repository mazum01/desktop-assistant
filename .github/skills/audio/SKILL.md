---
name: audio
description: >
  Control VERA audio settings: output volume, EQ preset, microphone input gain,
  mute/unmute, repeat-last-spoken, and active audio backend. Use for audio tuning and backend selection.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Audio Skill

Control VERA's consolidated audio controls.

## When to use

- "Set volume to 70%"
- "What EQ preset is active?"
- "Set EQ to vocal"
- "Increase microphone gain"
- "Mute audio" / "Unmute audio"
- "Repeat what you just said"
- "What backend are you using?"
- "Switch backend to respeaker"
- "Show voice STT status"
- "Enable STT" / "Disable STT"
- "Change wake backend to openwakeword"
- "Set wake word threshold to 0.6"

## Commands

| Command              | Description |
|----------------------|-------------|
| `status`             | Show backend, volume, EQ, and input gain |
| `volume [0-100]`     | Get or set output volume |
| `mute [on|off]`      | Get mute state, or mute/unmute output |
| `repeat`             | Repeat the last spoken phrase |
| `eq [preset]`        | Get or set EQ preset |
| `input-gain [0-100]` | Get or set microphone gain |
| `backend [name]`     | Get or set backend (`default`, `respeaker_flex`) |
| `stt`                | Show voice STT and wake-word status |
| `stt on\|off`        | Enable or disable voice STT pipeline |
| `stt key=value...`   | Update STT/wake settings (see keys below) |

### STT/Wake setting keys for `stt key=value...`

| Key | Description | Example |
|-----|-------------|---------|
| `wake_backend` | Wake detector: `energy` or `openwakeword` | `wake_backend=openwakeword` |
| `oww_model` | openWakeWord model name (without `.onnx`) | `oww_model=hey_jarvis_v0.1` |
| `oww_threshold` | OWW score threshold 0.0–1.0 (lower = more sensitive) | `oww_threshold=0.5` |
| `oww_refractory_s` | Seconds to suppress re-triggers after wake | `oww_refractory_s=2.0` |
| `wake_threshold_dbfs` | Energy wake threshold in dBFS (energy backend only) | `wake_threshold_dbfs=-35` |
| `wake_cooldown_s` | Cooldown after any wake event (seconds) | `wake_cooldown_s=1.5` |
| `stt_backend` | STT engine: `faster_whisper`, `shell`, or `null` | `stt_backend=faster_whisper` |
| `stt_model` | Whisper model size | `stt_model=base.en` |
| `stt_language` | Recognition language | `stt_language=en` |
| `stt_timeout_s` | Max seconds before STT aborts | `stt_timeout_s=60` |
| `stt_cpu_threads` | CPU threads for Whisper inference | `stt_cpu_threads=2` |
| `enabled` | Enable/disable entire voice pipeline | `enabled=true` |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/audio/audio.py <command> [args]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/audio/audio.py status
python3 ~/.openclaw/workspace/skills/audio/audio.py volume 75
python3 ~/.openclaw/workspace/skills/audio/audio.py mute on
python3 ~/.openclaw/workspace/skills/audio/audio.py repeat
python3 ~/.openclaw/workspace/skills/audio/audio.py eq vocal
python3 ~/.openclaw/workspace/skills/audio/audio.py input-gain 70
python3 ~/.openclaw/workspace/skills/audio/audio.py backend respeaker_flex
python3 ~/.openclaw/workspace/skills/audio/audio.py stt
python3 ~/.openclaw/workspace/skills/audio/audio.py stt on
python3 ~/.openclaw/workspace/skills/audio/audio.py stt wake_backend=openwakeword oww_model=hey_jarvis_v0.1 oww_threshold=0.5
python3 ~/.openclaw/workspace/skills/audio/audio.py stt stt_backend=faster_whisper stt_language=en
```

Relay the returned JSON in natural language.
