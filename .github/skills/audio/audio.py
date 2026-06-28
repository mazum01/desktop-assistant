#!/usr/bin/env python3
"""Control VERA audio settings (volume, EQ, input gain, mute, repeat, backend, STT)."""

import json
import sys
import urllib.error
import urllib.request

BASE_URL = "http://localhost:8080"
EQ_PRESETS = ("flat", "bass_boost", "treble_boost", "vocal", "loudness", "warm", "custom")
COMMANDS = ("status", "volume", "mute", "repeat", "eq", "input-gain", "backend", "stt")


def _get(path: str) -> dict:
    with urllib.request.urlopen(f"{BASE_URL}{path}", timeout=5) as resp:
        return json.loads(resp.read())


def _put(path: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _post(path: str, body: dict | None = None) -> dict:
    data = json.dumps(body or {}).encode()
    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(json.dumps({
            "ok": False,
            "error": f"Usage: audio.py <command> [args]\nCommands: {', '.join(COMMANDS)}",
        }))
        sys.exit(1)

    cmd = sys.argv[1]

    try:
        if cmd == "status":
            audio = _get("/api/settings/audio")
            vol = _get("/api/music/volume")
            eq = _get("/api/music/eq")
            gain = _get("/api/audio/input-gain")
            mute = _get("/api/audio/mute")
            print(json.dumps({
                "ok": True,
                "backend": audio.get("backend", "default"),
                "volume": vol.get("level", -1),
                "muted": mute.get("muted", False),
                "eq_preset": eq.get("preset", "flat"),
                "input_gain": gain.get("level"),
                "input_gain_available": gain.get("available", False),
            }))

        elif cmd == "volume":
            if len(sys.argv) < 3:
                d = _get("/api/music/volume")
                print(json.dumps({"ok": True, "volume": d.get("level", -1)}))
            else:
                level = int(sys.argv[2])
                if not (0 <= level <= 100):
                    raise ValueError("Volume must be 0-100")
                _put("/api/music/volume", {"level": level})
                print(json.dumps({"ok": True, "volume": level}))

        elif cmd == "mute":
            if len(sys.argv) < 3:
                d = _get("/api/audio/mute")
                print(json.dumps({"ok": True, "muted": d.get("muted", False)}))
            else:
                state = sys.argv[2].lower()
                if state not in ("on", "off"):
                    raise ValueError("Mute state must be on or off")
                muted = state == "on"
                _put("/api/audio/mute", {"muted": muted})
                print(json.dumps({"ok": True, "muted": muted}))

        elif cmd == "repeat":
            d = _post("/api/audio/repeat", {})
            print(json.dumps(d))

        elif cmd == "eq":
            if len(sys.argv) < 3:
                d = _get("/api/music/eq")
                print(json.dumps({"ok": True, "preset": d.get("preset", "flat"), "presets": d.get("presets", list(EQ_PRESETS))}))
            else:
                preset = sys.argv[2]
                if preset not in EQ_PRESETS:
                    raise ValueError(f"Invalid preset: {preset}")
                _put("/api/music/eq", {"preset": preset})
                print(json.dumps({"ok": True, "preset": preset}))

        elif cmd == "input-gain":
            if len(sys.argv) < 3:
                d = _get("/api/audio/input-gain")
                print(json.dumps({
                    "ok": d.get("ok", True),
                    "available": d.get("available", False),
                    "input_gain": d.get("level"),
                }))
            else:
                level = int(sys.argv[2])
                if not (0 <= level <= 100):
                    raise ValueError("Input gain must be 0-100")
                _put("/api/audio/input-gain", {"level": level})
                print(json.dumps({"ok": True, "input_gain": level}))

        elif cmd == "backend":
            if len(sys.argv) < 3:
                d = _get("/api/settings/audio")
                print(json.dumps({"ok": True, "backend": d.get("backend", "default")}))
            else:
                backend = sys.argv[2]
                if backend not in ("default", "respeaker_flex"):
                    raise ValueError("Backend must be default or respeaker_flex")
                _put("/api/settings/audio", {"backend": backend})
                print(json.dumps({
                    "ok": True,
                    "backend": backend,
                    "message": "Backend saved. Restart daemon to apply.",
                }))

        elif cmd == "stt":
            if len(sys.argv) < 3:
                d = _get("/api/settings/voice")
                print(json.dumps({
                    "ok": True,
                    "enabled": d.get("enabled", False),
                    "stt_backend": d.get("stt_backend", "shell"),
                    "stt_language": d.get("stt_language", "en"),
                    "stt_command": d.get("stt_command", ""),
                }))
            else:
                arg = sys.argv[2].lower()
                if arg in ("on", "off"):
                    enabled = arg == "on"
                    d = _put("/api/settings/voice", {"enabled": enabled})
                    print(json.dumps({
                        "ok": True,
                        "enabled": d.get("enabled", enabled),
                    }))
                else:
                    patch = {}
                    for item in sys.argv[2:]:
                        if "=" not in item:
                            raise ValueError(
                                "stt set mode uses key=value pairs "
                                "(e.g. stt_backend=shell stt_language=en)"
                            )
                        key, value = item.split("=", 1)
                        key = key.strip()
                        value = value.strip()
                        if value.lower() in ("true", "false"):
                            patch[key] = value.lower() == "true"
                        else:
                            try:
                                patch[key] = int(value)
                            except ValueError:
                                try:
                                    patch[key] = float(value)
                                except ValueError:
                                    patch[key] = value
                    d = _put("/api/settings/voice", patch)
                    print(json.dumps({
                        "ok": True,
                        "enabled": d.get("enabled", False),
                        "stt_backend": d.get("stt_backend", "shell"),
                        "stt_language": d.get("stt_language", "en"),
                        "stt_command": d.get("stt_command", ""),
                    }))

    except urllib.error.URLError as exc:
        print(json.dumps({"ok": False, "error": f"Cannot reach assistant: {exc}"}))
        sys.exit(1)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(1)


if __name__ == "__main__":
    main()
