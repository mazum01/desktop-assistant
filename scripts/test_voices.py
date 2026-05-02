#!/usr/bin/env python3
"""A/B voice comparison: amy-medium vs lessac-high (TNG-computer tuned)."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.audio.tts import TextToSpeech, TTSConfig
from src.audio.output import AudioOutput

TEXT = "Desktop assistant online. All systems nominal. Good morning."

out = AudioOutput()

print("\n--- Voice 1: Amy (medium) ---")
tts_amy = TextToSpeech(TTSConfig(piper_voice_name="en_US-amy-medium"))
tts_amy.say(TEXT, output=out)

input("\nPress Enter for Voice 2 (Lessac / TNG-style)...")

print("\n--- Voice 2: Lessac (high) — TNG-computer tuned ---")
tts_tng = TextToSpeech(TTSConfig(
    piper_voice_name="en_US-lessac-high",
    piper_length_scale=1.15,
    piper_noise_scale=0.3,
    piper_noise_w=0.5,
))
tts_tng.say(TEXT, output=out)

input("\nPress Enter to hear Lessac at default settings (less TNG)...")

print("\n--- Voice 2b: Lessac (high) — default expressiveness ---")
tts_tng2 = TextToSpeech(TTSConfig(piper_voice_name="en_US-lessac-high"))
tts_tng2.say(TEXT, output=out)

print("\nDone. Edit config/settings.yaml to set your preferred voice.")
