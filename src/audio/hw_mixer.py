"""
reSpeaker hardware mixer control.

The reSpeaker Flex XVF3800 exposes an ALSA hardware playback gain control
(``PCM``) that is independent of the PipeWire software volume.  On a fresh
boot this control can sit well below 0 dB (observed at -29 dB), which makes
the speaker very quiet regardless of the PipeWire/application volume.

This module finds the reSpeaker card by name (its ALSA card index is not
stable across reboots / USB re-enumeration) and pins the hardware playback
control to maximum so the only volume attenuation happens in the PipeWire
graph (where the EQ and per-app volumes live).

All operations are best-effort: failures are logged and swallowed so a
missing/renamed control never breaks audio startup.
"""

from __future__ import annotations

import logging
import re
import subprocess
from typing import Optional

log = logging.getLogger(__name__)

# Substrings that identify the reSpeaker card in /proc/asound/cards.
_CARD_MATCH = ("reSpeaker", "L16K6Ch", "XVF3800")

# Playback simple-control name on the XVF3800 and the level to set
# (the control range is 0-60; 60 == 0 dB == unity).
_PLAYBACK_CONTROL = "PCM"


def _find_card_index() -> Optional[int]:
    """Return the ALSA card index of the reSpeaker, or None if not present."""
    try:
        with open("/proc/asound/cards", "r") as fh:
            text = fh.read()
    except Exception as exc:
        log.debug("hw_mixer: cannot read /proc/asound/cards: %s", exc)
        return None
    # Lines look like: " 2 [L16K6Ch        ]: USB-Audio - reSpeaker Flex ..."
    for line in text.splitlines():
        if any(m in line for m in _CARD_MATCH):
            m = re.match(r"\s*(\d+)\s", line)
            if m:
                return int(m.group(1))
    return None


def _amixer_set_max(card: int, control: str) -> bool:
    """Set an amixer simple control to 100% on *card*.  Returns True on success."""
    try:
        r = subprocess.run(
            ["amixer", "-c", str(card), "sset", control, "100%", "unmute"],
            capture_output=True, text=True, timeout=5,
        )
        return r.returncode == 0
    except Exception as exc:
        log.debug("hw_mixer: amixer sset %s failed: %s", control, exc)
        return False


def ensure_max_playback_gain() -> bool:
    """Pin the reSpeaker hardware playback gain to maximum (0 dB).

    Best-effort: returns True if the control was set, False otherwise.
    Safe to call repeatedly (e.g. at every service start).
    """
    card = _find_card_index()
    if card is None:
        log.info("hw_mixer: reSpeaker card not found — skipping HW gain setup")
        return False
    ok = _amixer_set_max(card, _PLAYBACK_CONTROL)
    if ok:
        log.info("hw_mixer: reSpeaker (card %d) playback gain pinned to max", card)
    else:
        log.warning("hw_mixer: failed to set reSpeaker (card %d) playback gain", card)
    return ok
