#!/usr/bin/env python3
"""
Microphone bring-up — record 5 seconds and report level meter.

Run on the Pi:
    python3 scripts/test_microphone.py

Uses the system default input. Prints peak level so you can verify the
mic is wired and gain is sane. Saves the recording to /tmp/mic_test.wav.
"""

import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.audio.input import AudioInput, AudioInputConfig

print("╔══════════════════════════════════════════════════╗")
print("║   Desktop Assistant — Microphone bring-up test   ║")
print("╚══════════════════════════════════════════════════╝")
print()

# ── List input devices ────────────────────────────────────────────────
print("── Input devices ──")
try:
    import sounddevice as sd
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            print(f"  [{idx:2}] {dev['name']}  ({dev['max_input_channels']} ch)")
except Exception as exc:
    print(f"  ERROR querying devices: {exc}")
    sys.exit(1)

print()
print("── Recording 5 seconds (speak now) ──")
mic = AudioInput(AudioInputConfig(sample_rate=16000, channels=1))
samples = mic.record(seconds=5.0)

# ── Stats ─────────────────────────────────────────────────────────────
peak = float(np.max(np.abs(samples)))
rms = float(np.sqrt(np.mean(samples ** 2)))
db_peak = 20 * np.log10(peak + 1e-9)
db_rms = 20 * np.log10(rms + 1e-9)

print()
print("── Capture stats ──")
print(f"  Samples : {len(samples):,}")
print(f"  Peak    : {peak:.4f}  ({db_peak:+.1f} dBFS)")
print(f"  RMS     : {rms:.4f}  ({db_rms:+.1f} dBFS)")

if peak < 0.001:
    print("  ⚠ Level very low — mic may not be connected, or system input is muted.")
    print("    Check: alsamixer  (F4 to switch to capture)")
elif peak > 0.95:
    print("  ⚠ Clipping detected — reduce gain.")
else:
    print("  ✓ Level looks reasonable")

# ── Save to WAV ───────────────────────────────────────────────────────
import wave
wav_path = "/tmp/mic_test.wav"
pcm = (samples * 32767).astype(np.int16)
with wave.open(wav_path, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(16000)
    wf.writeframes(pcm.tobytes())
print()
print(f"  ✓ Saved recording to {wav_path}")
print(f"    Play it back: aplay {wav_path}")
