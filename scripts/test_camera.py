#!/usr/bin/env python3
"""
Camera bring-up test — Raspberry Pi Camera Module 3 Wide.

Usage:
    python3 scripts/test_camera.py                  # default: capture + still
    python3 scripts/test_camera.py --preview        # live preview window
    python3 scripts/test_camera.py --preview 10     # preview for 10 seconds
    python3 scripts/test_camera.py --null           # capture only, no still

Default mode: enumerate, grab one frame, save still to /tmp/camera_test.jpg
Preview mode: open a Qt window showing live video (requires desktop session)
"""

import argparse
import os
import sys
import time

# ── Args ──────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Camera bring-up test")
parser.add_argument(
    "--preview", nargs="?", const=15, type=int, default=None,
    metavar="SECONDS",
    help="Show live preview window (default 15 seconds; press Ctrl-C to exit)",
)
parser.add_argument(
    "--null", action="store_true",
    help="Skip the still capture step",
)
parser.add_argument(
    "--index", type=int, default=0,
    help="Camera slot index (default 0)",
)
args = parser.parse_args()

print("╔══════════════════════════════════════════════════╗")
print("║     Desktop Assistant — Camera bring-up test    ║")
print("╚══════════════════════════════════════════════════╝")
print()

# ── 1. Check picamera2 ────────────────────────────────────────────────
print("── picamera2 check ──")
try:
    from picamera2 import Picamera2, Preview
    print("  ✓ picamera2 imported")
except ImportError:
    print("  ERROR: picamera2 not found.")
    print()
    print("  Install it via apt (do NOT use pip — libcamera has no PyPI wheel):")
    print("    sudo apt-get install -y python3-picamera2")
    sys.exit(1)

# ── 2. Enumerate cameras ──────────────────────────────────────────────
print()
print("── Camera enumeration ──")
cameras = Picamera2.global_camera_info()
if not cameras:
    print("  ERROR: No cameras detected.")
    print("  Check:")
    print("    1. Camera ribbon cable fully seated at both ends")
    print("    2. /boot/firmware/config.txt has 'camera_auto_detect=1'")
    sys.exit(1)

for cam in cameras:
    print(f"  Found camera: {cam}")

# ─────────────────────────────────────────────────────────────────────
# PREVIEW MODE — uses Picamera2 directly with a Qt window
# ─────────────────────────────────────────────────────────────────────
if args.preview is not None:
    print()
    print(f"── Live preview ({args.preview} s) ──")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("  WARNING: no $DISPLAY / $WAYLAND_DISPLAY — preview needs a")
        print("  desktop session. If you're on SSH, try:")
        print("    DISPLAY=:0 python3 scripts/test_camera.py --preview")
        print()

    # QtGL needs XRGB8888 (4-channel); Qt-software and DRM accept RGB888.
    # Try backends in order, each with its preferred pixel format.
    backends = [
        (Preview.QTGL, "QtGL",          "XRGB8888"),
        (Preview.QT,   "Qt (software)", "RGB888"),
        (Preview.DRM,  "DRM (no desktop)", "RGB888"),
    ]

    last_error = None
    for backend, name, fmt in backends:
        picam2 = Picamera2(args.index)
        config = picam2.create_preview_configuration(
            main={"size": (1280, 720), "format": fmt}
        )
        picam2.configure(config)
        try:
            picam2.start_preview(backend)
        except Exception as exc:
            print(f"  · {name} unavailable ({exc.__class__.__name__}: {exc})")
            picam2.close()
            last_error = exc
            continue

        print(f"  ✓ Preview backend: {name} (format={fmt})")
        picam2.start()
        print(f"  Showing live video for {args.preview} s — Ctrl-C to exit early")
        try:
            time.sleep(args.preview)
        except KeyboardInterrupt:
            print("\n  Interrupted")
        finally:
            picam2.stop()
            picam2.close()
        break
    else:
        print(f"  ERROR: no preview backend worked. Last error: {last_error}")
        print("  Install:  sudo apt-get install -y python3-pyqt5")
        sys.exit(1)

    print()
    print("══════════════════════════════════════════════════")
    print("Live preview complete ✓")
    print("══════════════════════════════════════════════════")
    sys.exit(0)

# ─────────────────────────────────────────────────────────────────────
# DEFAULT MODE — open via project driver, capture, save still
# ─────────────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from src.vision.camera import Camera, CameraConfig

print()
print(f"── Opening camera slot {args.index} ──")
cfg = CameraConfig(index=args.index, width=1280, height=720, framerate=30)
cam = Camera(cfg)

if not cam.hardware_ready:
    print("  ERROR: Camera not initialised — check wiring and slot index.")
    sys.exit(1)

print("  ✓ Camera initialised")

print()
print("── Capture test ──")
cam.start()
print("  Camera started, waiting for auto-exposure...")
time.sleep(1.0)

frame = cam.capture_frame()
print(f"  ✓ Frame captured: shape={frame.shape}, dtype={frame.dtype}")

if not args.null:
    still_path = "/tmp/camera_test.jpg"
    print()
    print(f"── Saving still to {still_path} ──")
    cam.capture_still(still_path)
    if os.path.exists(still_path):
        size_kb = os.path.getsize(still_path) // 1024
        print(f"  ✓ Still saved ({size_kb} KB)")

cam.close()

print()
print("══════════════════════════════════════════════════")
print("Camera bring-up PASSED ✓")
print("  Try also:  python3 scripts/test_camera.py --preview")
print("══════════════════════════════════════════════════")
