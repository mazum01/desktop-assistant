---
name: music
description: >
  Control Pandora music playback on VERA via pianobar.
  Handles play, stop, skip, pause, station selection, thumbs-up/down,
  and status queries. Use for any music-related request.
metadata:
  openclaw:
    os: ["linux"]
    requires:
      bins: ["python3"]
---

# Music Skill

Control Pandora music playback on VERA.

## When to use

- "Play some music" / "Play jazz" / "Put on something relaxing"
- "Stop the music" / "Turn off music"
- "Skip this song" / "Next song" / "I don't like this"
- "Pause" / "Resume"
- "What's playing?" / "Music status"
- "I love this song" (thumbs up) / "Ban this song" (thumbs down)
- "List stations" / "What stations are available?"
- "Switch to station 3" / "Play the Rock station"

## Commands

| Command         | Description                              |
|-----------------|------------------------------------------|
| `status`        | Show current song, state, and metadata   |
| `play [id]`     | Start/resume playback, optional station ID |
| `stop`          | Stop playback                            |
| `next`          | Skip to next song                        |
| `pause`         | Toggle pause/resume                      |
| `thumbs-up`     | Love current song (Pandora thumbs up)    |
| `thumbs-down`   | Ban current song (Pandora thumbs down)   |
| `stations`      | List available Pandora stations with IDs |
| `station <id>`  | Switch to station by numeric ID          |

## How to invoke

```bash
python3 ~/.openclaw/workspace/skills/music/music.py <command> [args]
```

Examples:
```bash
python3 ~/.openclaw/workspace/skills/music/music.py status
python3 ~/.openclaw/workspace/skills/music/music.py play
python3 ~/.openclaw/workspace/skills/music/music.py station 3
```

On success prints JSON with `{"ok": true, ...}` plus relevant data.
For `status`, includes `state`, `song` (title, artist, album), and `configured`.
For `stations`, includes a list of `{"id": n, "name": "..."}`.

After running, relay the information to the user in natural language.
If music is not configured (Pandora not set up), the response will indicate
`"configured": false`.
