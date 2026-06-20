---
name: podcast
description: >
  Control Apple Podcasts on VERA: search, subscribe, list episodes,
  play, pause/resume, stop, and status.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Podcast Skill

Control Apple Podcasts subscriptions and playback.

## When to use

- "Find the Lex Fridman podcast"
- "Subscribe to The Daily"
- "List my podcasts"
- "Play episode 0 of podcast 12345"
- "Pause podcast" / "Resume podcast" / "Stop podcast"

## Commands

| Command | Description |
|---|---|
| `search <query>` | Search Apple Podcasts |
| `subscribe <query\|url>` | Subscribe by search text or RSS URL |
| `list` | List subscriptions |
| `episodes <id>` | List episodes for a subscription |
| `play <id> [index]` | Play episode by index (default 0 newest) |
| `pause` / `resume` / `stop` | Playback control |
| `seek <sec>` | Jump to absolute time in current episode |
| `skip <delta>` | Jump by +/- seconds |
| `back15` / `fwd30` | Quick jumps: -15s / +30s |
| `status` | Current playback state |
| `refresh <id>` | Refresh episodes from RSS |
| `unsubscribe <id>` | Remove subscription |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/podcast/podcast.py <command> [args]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/podcast/podcast.py search "the daily"
python3 ~/.openclaw/workspace/skills/podcast/podcast.py subscribe "the daily"
python3 ~/.openclaw/workspace/skills/podcast/podcast.py list
python3 ~/.openclaw/workspace/skills/podcast/podcast.py play 12345 0
```
