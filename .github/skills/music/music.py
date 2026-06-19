#!/usr/bin/env python3
"""Control Pandora music playback on VERA."""

import json
import sys
import urllib.request
import urllib.error

BASE_URL = "http://localhost:8080"
COMMANDS = ("status", "play", "stop", "next", "pause", "thumbs-up", "thumbs-down",
            "stations", "station")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}", data=data,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: music.py <command> [args]\nCommands: {', '.join(COMMANDS)}"
        }))
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        if cmd == "status":
            data = _get("/api/music/status")
            song = data.get("song", {})
            print(json.dumps({
                "ok": True,
                "state":    data.get("state", "unknown"),
                "song":     {
                    "title":  song.get("title", "—"),
                    "artist": song.get("artist", "—"),
                    "album":  song.get("album", "—"),
                },
                "volume":      data.get("volume", -1),
                "eq_preset":   data.get("eq_preset", "flat"),
                "configured":  data.get("configured", False),
            }))

        elif cmd == "stations":
            data = _get("/api/music/status")
            stations = data.get("stations", [])
            print(json.dumps({"ok": True, "stations": stations}))

        elif cmd == "play":
            body: dict = {}
            if len(sys.argv) >= 3:
                try:
                    body["station_id"] = int(sys.argv[2])
                except ValueError:
                    pass
            _post("/api/music/play", body)
            print(json.dumps({"ok": True, "message": "Music playing"}))

        elif cmd == "stop":
            _post("/api/music/stop")
            print(json.dumps({"ok": True, "message": "Music stopped"}))

        elif cmd == "next":
            _post("/api/music/next")
            print(json.dumps({"ok": True, "message": "Skipped to next song"}))

        elif cmd == "pause":
            _post("/api/music/pause")
            print(json.dumps({"ok": True, "message": "Music paused/resumed"}))

        elif cmd == "thumbs-up":
            _post("/api/music/thumbs-up")
            print(json.dumps({"ok": True, "message": "Thumbs up — song loved on Pandora"}))

        elif cmd == "thumbs-down":
            _post("/api/music/thumbs-down")
            print(json.dumps({"ok": True, "message": "Thumbs down — song banned on Pandora"}))

        elif cmd == "station":
            if len(sys.argv) < 3:
                print(json.dumps({"ok": False, "error": "Usage: music.py station <id>"}))
                sys.exit(1)
            try:
                station_id = int(sys.argv[2])
            except ValueError:
                print(json.dumps({"ok": False, "error": f"Invalid station ID: {sys.argv[2]!r}"}))
                sys.exit(1)
            _post("/api/music/station", {"station_id": station_id})
            print(json.dumps({"ok": True, "station_id": station_id,
                              "message": f"Switched to station {station_id}"}))

    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
