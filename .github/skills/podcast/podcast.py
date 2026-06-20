#!/usr/bin/env python3
"""Control Apple Podcasts subscriptions and playback on VERA."""

import json
import urllib.error
import urllib.parse
import urllib.request
import sys

BASE_URL = "http://localhost:8080"
COMMANDS = (
    "search", "subscribe", "list", "episodes", "play", "pause", "resume", "stop", "status", "refresh", "unsubscribe"
)


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=10) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _delete(path: str) -> dict:
    req = urllib.request.Request(f"{BASE_URL}{path}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": "Usage: podcast.py <command> [args]",
            "commands": list(COMMANDS),
        }))
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        if cmd == "search":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py search <query>")
            q = urllib.parse.quote_plus(" ".join(sys.argv[2:]))
            data = _get(f"/api/podcasts/search?q={q}&limit=10")
            print(json.dumps(data))
            return

        if cmd == "subscribe":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py subscribe <query|feed-url>")
            data = _post("/api/podcasts/subscribe", {"query_or_url": " ".join(sys.argv[2:])})
            print(json.dumps(data))
            return

        if cmd == "list":
            print(json.dumps(_get("/api/podcasts")))
            return

        if cmd == "episodes":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py episodes <podcast_id>")
            pid = sys.argv[2]
            print(json.dumps(_get(f"/api/podcasts/{pid}/episodes?limit=25")))
            return

        if cmd == "play":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py play <podcast_id> [episode_index]")
            pid = sys.argv[2]
            idx = int(sys.argv[3]) if len(sys.argv) >= 4 else 0
            print(json.dumps(_post("/api/podcasts/play", {"podcast_id": pid, "episode_index": idx})))
            return

        if cmd == "pause":
            print(json.dumps(_post("/api/podcasts/pause", {})))
            return

        if cmd == "resume":
            print(json.dumps(_post("/api/podcasts/resume", {})))
            return

        if cmd == "stop":
            print(json.dumps(_post("/api/podcasts/stop", {})))
            return

        if cmd == "status":
            print(json.dumps(_get("/api/podcasts/status")))
            return

        if cmd == "refresh":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py refresh <podcast_id>")
            pid = sys.argv[2]
            print(json.dumps(_post(f"/api/podcasts/{pid}/refresh", {})))
            return

        if cmd == "unsubscribe":
            if len(sys.argv) < 3:
                raise ValueError("Usage: podcast.py unsubscribe <podcast_id>")
            pid = sys.argv[2]
            print(json.dumps(_delete(f"/api/podcasts/{pid}")))
            return

    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
