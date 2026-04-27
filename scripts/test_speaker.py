#!/usr/bin/env python3
"""
Speaker / audio output bring-up — Sabrent USB adapter + stereo speakers.

Run on the Pi:
    python3 scripts/test_speaker.py

Plays a 440 Hz left-channel beep, then 880 Hz right-channel beep, then
a stereo sweep — so you can verify each channel and the wiring.
"""

import os
import sys
import time

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.audio.output import AudioOutput, AudioOutputConfig, find_output_device

print("╔══════════════════════════════════════════════════╗")
print("║   Desktop Assistant — Speaker bring-up test     ║")
print("╚══════════════════════════════════════════════════╝")
print()

# ── Enumerate output devices ──────────────────────────────────────────
print("── Output devices ──")
try:
    import sounddevice as sd
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] > 0:
            tag = "  ← Sabrent" if "sabrent" in dev["name"].lower() else ""
            print(f"  [{idx:2}] {dev['name']}{tag}")
except Exception as exc:
    print(f"  ERROR querying devices: {exc}")
    sys.exit(1)

idx = find_output_device("Sabrent")
if idx is None:
    print()
    print("  ✗ No Sabrent device found.")
    print("    Check: lsusb | grep -i sabrent")
    print("    The adapter must be plugged in BEFORE the script runs.")
    sys.exit(1)

print()
print(f"── Using device {idx} ──")

cfg = AudioOutputConfig(device_index=idx, sample_rate=48000, channels=2)
out = AudioOutput(cfg)
sr = cfg.sample_rate

def stereo_tone(freq, duration, channel: str):
    """Generate a tone on left, right, or both channels."""
    t = np.linspace(0, duration, int(sr * duration), endpoint=False)
    tone = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    silence = np.zeros_like(tone)
    if channel == "left":
        return np.column_stack([tone, silence])
    if channel == "right":
        return np.column_stack([silence, tone])
    return np.column_stack([tone, tone])

# ── Test 1: Left channel ──────────────────────────────────────────────
print("  → 440 Hz beep on LEFT channel (1 second)")
out.play(stereo_tone(440, 1.0, "left"))
time.sleep(0.3)

# ── Test 2: Right channel ─────────────────────────────────────────────
print("  → 880 Hz beep on RIGHT channel (1 second)")
out.play(stereo_tone(880, 1.0, "right"))
time.sleep(0.3)

# ── Test 3: Stereo sweep ──────────────────────────────────────────────
print("  → Stereo sweep 200 → 2000 Hz (2 seconds)")
t = np.linspace(0, 2.0, int(sr * 2.0), endpoint=False)
freq = np.linspace(200, 2000, len(t))
phase = np.cumsum(2 * np.pi * freq / sr)
tone = (0.2 * np.sin(phase)).astype(np.float32)
out.play(np.column_stack([tone, tone]))

print()
print("══════════════════════════════════════════════════")
print("Speaker test complete ✓")
print("If you heard:")
print("  • Left tone only  → left wiring OK")
print("  • Right tone only → right wiring OK")
print("  • Sweep on both   → stereo output OK")
print("══════════════════════════════════════════════════")
