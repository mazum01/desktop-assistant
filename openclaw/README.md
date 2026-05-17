# OpenClaw Skills

Natural-language skills for the [OpenClaw](https://openclaw.dev) AI gateway,
enabling Telegram/Claude to control the desktop assistant.

## Installation

Link or copy the skill directories into your OpenClaw workspace:

```bash
cp -r openclaw/skills/pan-camera  ~/.openclaw/workspace/skills/
cp -r openclaw/skills/grab-frame  ~/.openclaw/workspace/skills/
```

After copying, restart the OpenClaw gateway to pick up the new skills:

```bash
systemctl --user restart openclaw-gateway.service
```

> **Tip:** The gateway caches skill snapshots per session. If a skill does not
> appear after restart, clear `skillsSnapshot` from
> `~/.openclaw/agents/main/sessions/sessions.json` while the gateway is stopped,
> then start it again.

## Skills

### `pan-camera`
Pan the camera head to a specific angle. Responds to natural-language requests
like "look left", "look right", "face me", "pan to 135 degrees".

**Requires:** `da` CLI + ZMQ IPC bridge running (`desktop-assistant-core.service`)

### `grab-frame`
Take a full-resolution still photo from camera 1 or camera 2. Saves a
timestamped JPEG to `~/Pictures/desktop-assistant/`.

**Requires:** `desktop-assistant-core.service` running (uses `/api/snapshot` HTTP endpoint)

## Prerequisites

- OpenClaw gateway installed and configured (`~/.openclaw/openclaw.json`)
- `desktop-assistant-core.service` active
- `python3` with `pyzmq` available (for `pan-camera`)
