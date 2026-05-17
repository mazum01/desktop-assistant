#!/usr/bin/env python3
"""Grab a full-resolution JPEG still from the desktop-assistant camera."""

import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

BASE_URL = "http://localhost:8080"
SAVE_DIR = Path.home() / "Pictures" / "desktop-assistant"
VALID_CAMERAS = {"1", "2"}

STREAM_URLS = {
    "1": f"{BASE_URL}/stream",
    "2": f"{BASE_URL}/stream2",
}


def extract_jpeg(data: bytes) -> bytes | None:
    """Extract the first complete JPEG frame from a multipart MJPEG stream."""
    start = data.find(b"\xff\xd8")
    if start < 0:
        return None
    end = data.find(b"\xff\xd9", start)
    if end < 0:
        return None
    return data[start : end + 2]


def main():
    camera = sys.argv[1] if len(sys.argv) > 1 else "1"
    if camera not in VALID_CAMERAS:
        print(json.dumps({"ok": False, "error": f"Invalid camera '{camera}'. Use 1 or 2."}))
        sys.exit(1)

    SAVE_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = SAVE_DIR / f"cam{camera}_{timestamp}.jpg"

    url = STREAM_URLS[camera]
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=5) as resp:
            # Read enough data to get at least one full frame (~250KB should be plenty)
            data = resp.read(512 * 1024)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"Request failed: {exc}"}))
        sys.exit(1)

    jpeg = extract_jpeg(data)
    if not jpeg:
        print(json.dumps({"ok": False, "error": "Could not extract JPEG frame from stream"}))
        sys.exit(1)

    out_path.write_bytes(jpeg)
    size_kb = len(jpeg) // 1024

    print(json.dumps({
        "ok": True,
        "camera": int(camera),
        "path": str(out_path),
        "size_kb": size_kb,
        "message": f"Saved {size_kb} KB to {out_path}",
    }))


if __name__ == "__main__":
    main()
