#!/usr/bin/env python3
"""Play the boot startup chime through the live USB DAC.

Mono signal duplicated to both channels — works even with only one
speaker wired (right channel today). After the chime, speak the
current version.
"""
from __future__ import annotations

import sys
import time

from src.audio.output import AudioOutput
from src.audio.tts import TextToSpeech
from src.audio.version_announcer import VersionAnnouncer


def main() -> int:
    out = AudioOutput()
    print(f"hardware_ready={out.hardware_ready} device={getattr(out, '_device_index', '?')}")
    print("Playing boot chime (C5-E5-G5)…")
    out.chime()
    time.sleep(0.2)
    print("Speaking version…")
    tts = TextToSpeech()
    VersionAnnouncer(tts=tts, audio_output=out).announce_startup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
