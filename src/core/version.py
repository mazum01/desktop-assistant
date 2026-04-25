"""
Version accessor for Desktop Assistant.

Reads the VERSION file at the repo root so every service and the TTS
startup/query handler always reports the correct version without
hardcoding it.
"""

from __future__ import annotations

import pathlib

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_VERSION_FILE = _REPO_ROOT / "VERSION"


def get_version() -> str:
    """Return the current version string (e.g. '0.1.0')."""
    return _VERSION_FILE.read_text(encoding="utf-8").strip()


def spoken_version() -> str:
    """Return a human-friendly spoken version string.

    Example: '0.1.0' → 'version zero point one point zero'
    """
    raw = get_version().lstrip("v")
    word_map = {
        "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
        "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    }

    def _part_to_words(part: str) -> str:
        if part.isdigit() and len(part) == 1:
            return word_map[part]
        # multi-digit part: spell out each digit
        return " ".join(word_map.get(c, c) for c in part)

    # split on dots and hyphens (e.g. "0.2.0-dev" → ["0","2","0","dev"])
    import re
    segments = re.split(r"[.\-]", raw)
    return "version " + " point ".join(_part_to_words(s) for s in segments)


if __name__ == "__main__":
    print(f"Version: {get_version()}")
    print(f"Spoken:  {spoken_version()}")
