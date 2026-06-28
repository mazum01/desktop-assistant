---
name: audio
description: >
  Control VERA audio settings: output volume, EQ preset, microphone input gain,
  mute/unmute, repeat-last-spoken, and active audio backend.
  Use for audio tuning and backend selection.
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

## Commands

| Command             | Description |
|---------------------|-------------|
| `status`            | Show backend, volume, EQ, and input gain |
| `volume [0-100]`    | Get or set output volume |
| `mute [on|off]`     | Get mute state, or mute/unmute output |
| `repeat`            | Repeat the last spoken phrase |
| `eq [preset]`       | Get or set EQ preset |
| `input-gain [0-100]`| Get or set microphone gain |
| `backend [name]`    | Get or set backend (`default`, `respeaker_flex`) |
| `stt`               | Show voice STT status |
| `stt on|off`        | Enable or disable voice STT pipeline |
| `stt key=value...`  | Update STT settings (e.g., backend/language/command) |

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
python3 ~/.openclaw/workspace/skills/audio/audio.py stt stt_backend=shell stt_language=en stt_command='whisper-cli -m /models/base.en.bin -f {wav_path} -l {language}'
```

Relay the returned JSON in natural language.
