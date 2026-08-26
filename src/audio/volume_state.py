"""Persisted system output volume — single source of truth.

The user's chosen output volume is stored as a plain 0–100 integer in
``~/.config/desktop-assistant/music_volume.txt``.

Two independent processes touch the sink volume:

* ``MusicService`` (media unit) restores the saved level on start and
  writes the file whenever the volume is changed.
* ``pipewire_eq`` (core unit) re-elects the DA Equalizer sink as default
  at start and after every EQ preset change, and has to give that sink a
  volume at the same time.

Both read the level from here so the core unit can no longer clobber the
user's setting with a hardcoded 100%.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".config" / "desktop-assistant"
VOLUME_FILE = STATE_DIR / "music_volume.txt"


def load_volume() -> Optional[int]:
    """Return the persisted volume as 0–100, or None when unset/invalid."""
    try:
        if not VOLUME_FILE.exists():
            return None
        level = int(VOLUME_FILE.read_text().strip())
    except Exception as exc:
        log.warning("volume_state: failed to read persisted volume: %s", exc)
        return None
    if not 0 <= level <= 100:
        log.warning("volume_state: persisted volume %d out of range", level)
        return None
    return level


def save_volume(level: int) -> None:
    """Persist *level* (0–100) so it survives daemon restarts and reboots."""
    level = max(0, min(100, int(level)))
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        VOLUME_FILE.write_text(str(level))
    except Exception as exc:
        log.warning("volume_state: failed to persist volume: %s", exc)


def load_scalar(default: float = 1.0) -> float:
    """Return the persisted volume as a PipeWire scalar (0.0–1.0).

    Falls back to *default* when nothing has been persisted yet.
    """
    level = load_volume()
    return default if level is None else level / 100.0
