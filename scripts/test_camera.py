#!/usr/bin/env python3
"""
Camera bring-up test — Raspberry Pi Camera Module 3 Wide, slot 0.

Run on the Pi (system Python, no venv):
    python3 scripts/test_camera.py

Saves a test frame to /tmp/camera_test.jpg if the camera is present.
"""

import sys
import time

print("╔══════════════════════════════════════════════════╗")
print("║     Desktop Assistant — Camera bring-up test    ║")
print("╚══════════════════════════════════════════════════╝")
print()

# ── 1. Check picamera2 ────────────────────────────────────────────────
print("── picamera2 check ──")
try:
    from picamera2 import Picamera2
    print("  ✓ picamera2 imported")
except ImportError:
    print("  ERROR: picamera2 not found.")
    print()
    print("  Install it via apt (do NOT use pip — libcamera has no PyPI wheel):")
    print("    sudo apt-get install -y python3-picamera2")
    print()
    print("  If you're stuck inside an old venv, deactivate it and run again:")
    print("    deactivate")
    print("    python3 scripts/test_camera.py")
    sys.exit(1)

# ── 2. Enumerate cameras ──────────────────────────────────────────────
print()
print("── Camera enumeration ──")
cameras = Picamera2.global_camera_info()
if not cameras:
    print("  ERROR: No cameras detected.")
    print("  Check:")
    print("    1. Camera ribbon cable fully seated at both ends")
    print("    2. Camera enabled in raspi-config → Interface Options → Camera")
    print("    3. /boot/firmware/config.txt has 'camera_auto_detect=1'")
    sys.exit(1)

for cam in cameras:
    print(f"  Found camera: {cam}")

# ── 3. Open and test camera 0 ─────────────────────────────────────────
print()
print("── Opening camera slot 0 ──")

# Add project root to path
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.vision.camera import Camera, CameraConfig

cfg = CameraConfig(index=0, width=1280, height=720, framerate=30)
cam = Camera(cfg)

if not cam.hardware_ready:
    print("  ERROR: Camera not initialised — check wiring and slot index.")
    sys.exit(1)

print("  ✓ Camera initialised")

# ── 4. Start and capture ──────────────────────────────────────────────
print()
print("── Capture test ──")
cam.start()
print("  Camera started, waiting for auto-exposure...")
time.sleep(1.0)

frame = cam.capture_frame()
print(f"  ✓ Frame captured: shape={frame.shape}, dtype={frame.dtype}")

# ── 5. Save still ─────────────────────────────────────────────────────
still_path = "/tmp/camera_test.jpg"
print()
print(f"── Saving still to {still_path} ──")
cam.capture_still(still_path)
if os.path.exists(still_path):
    size_kb = os.path.getsize(still_path) // 1024
    print(f"  ✓ Still saved ({size_kb} KB)")
else:
    print("  WARNING: still file not found after capture")

cam.close()

print()
print("══════════════════════════════════════════════════")
print("Camera bring-up PASSED ✓")
print(f"  View the still: eog {still_path}")
print("══════════════════════════════════════════════════")
