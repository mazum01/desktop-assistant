# OpenClaw / Copilot Skills

This directory contains skills for the Desktop Assistant.

| Skill | Description | Runtime location |
|-------|-------------|-----------------|
| `restart-daemon` | Restart the systemd service and verify it's live | Copilot CLI only |
| `pan-camera` | Pan the camera servo to a given angle via Telegram/OpenClaw | `~/.openclaw/workspace/skills/pan-camera/` |
| `grab-frame` | Capture a full-resolution JPEG still from cam1 or cam2 | `~/.openclaw/workspace/skills/grab-frame/` |

## Deploying OpenClaw skills

After editing a skill here, sync it to the OpenClaw workspace:

```bash
cp .github/skills/pan-camera/{SKILL.md,pan_camera.py} ~/.openclaw/workspace/skills/pan-camera/
cp .github/skills/grab-frame/{SKILL.md,grab_frame.py} ~/.openclaw/workspace/skills/grab-frame/
```

OpenClaw's skill snapshot cache may need clearing after adding a **new** skill:
1. Stop gateway: `systemctl --user stop openclaw-gateway`
2. Edit `~/.openclaw/agents/main/sessions/sessions.json` — delete `skillsSnapshot` keys
3. Restart: `systemctl --user start openclaw-gateway`
