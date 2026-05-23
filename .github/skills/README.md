# VERA Skills

This directory contains skills for two systems:

- **GitHub Copilot CLI** (`.github/skills/`) — invoked by the VS Code Copilot agent
- **OpenClaw** (`~/.openclaw/workspace/skills/`) — natural-language Telegram/Claude skills

---

## OpenClaw Skills

### Installation

Copy skill directories into the OpenClaw workspace:

```bash
for skill in say describe-scene music face-tracking system-status quiet-hours object-detection pan-camera grab-frame; do
    cp -r .github/skills/$skill ~/.openclaw/workspace/skills/
done
```

Then restart the OpenClaw gateway:

```bash
systemctl --user restart openclaw-gateway.service
```

> **Tip:** If a new skill doesn't appear after restart, clear `skillsSnapshot`
> from `~/.openclaw/agents/main/sessions/sessions.json` while the gateway is
> stopped, then start it again.

---

### Skill Reference

| Skill               | Description                                               | Example phrases                                      |
|---------------------|-----------------------------------------------------------|------------------------------------------------------|
| `say`               | Speak any text via TTS                                    | "Say good morning", "Announce dinner is ready"       |
| `describe-scene`    | Describe what the camera sees, spoken aloud               | "What do you see?", "Describe the room"              |
| `music`             | Pandora: play/stop/skip/pause/thumbs/volume/stations      | "Play jazz", "Skip this song", "Volume 70"           |
| `face-tracking`     | Enable/disable auto face-following servo                  | "Follow me", "Hold still", "Is tracking on?"        |
| `system-status`     | Health: CPU, memory, temp, FPS, services                  | "How are you?", "What's your temperature?"           |
| `quiet-hours`       | Enable/disable/configure TTS silence window               | "Enable quiet mode", "Set quiet hours 10pm–7am"     |
| `object-detection`  | Enable/disable Hailo-8 COCO object classifier             | "Enable object detection", "What objects do you see?"|
| `pan-camera`        | Pan servo to angle or named direction                     | "Look left", "Face me", "Pan to 135°"                |
| `grab-frame`        | Capture full-res JPEG from camera 1 or 2                 | "Take a photo", "Snapshot from camera 2"             |

---

## Copilot CLI Skills

| Skill            | Description                                       |
|------------------|---------------------------------------------------|
| `restart-daemon` | Restart `desktop-assistant-core.service` + verify |

