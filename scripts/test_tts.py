#!/usr/bin/env python3
"""
TTS + spoken-version bring-up.

Run on the Pi:
    python3 scripts/test_tts.py

Plays:
  1. A test phrase ("Hello, I am the desktop assistant.")
  2. The startup version announcement
  3. A simulated "what version are you" response

Routes audio through the Sabrent USB adapter when available.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.audio.output import AudioOutput, find_output_device
from src.audio.tts import TextToSpeech
from src.audio.version_announcer import VersionAnnouncer
from src.core.version import get_version, spoken_version

print("╔══════════════════════════════════════════════════╗")
print("║   Desktop Assistant — TTS bring-up test         ║")
print("╚══════════════════════════════════════════════════╝")
print()
print(f"  Project version : {get_version()}")
print(f"  Spoken form     : {spoken_version()}")
print()

# Use a USB audio adapter if present, else system default
out = None
idx = find_output_device(("USB Audio", "C-Media", "Sabrent"))
if idx is not None:
    out = AudioOutput()
    import sounddevice as sd
    name = sd.query_devices(idx).get("name", "?")
    print(f"  Audio routed through device {idx} ({name})")
else:
    print("  Audio routed through ALSA default (no USB DAC detected)")

tts = TextToSpeech()
if not tts.hardware_ready:
    print()
    print("  ✗ espeak-ng not installed.  sudo apt-get install -y espeak-ng")
    sys.exit(1)

print()
print("── Test 1: greeting ──")
tts.say("Hello. I am the desktop assistant.", output=out)

print("── Test 2: startup announcement (FR-VR1) ──")
ann = VersionAnnouncer(tts=tts, audio_output=out)
ann.announce_startup()

print("── Test 3: verbal version query (FR-VR2) ──")
ann.announce_on_request()

print()
print("══════════════════════════════════════════════════")
print("TTS test complete ✓")
print("══════════════════════════════════════════════════")
