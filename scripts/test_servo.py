#!/usr/bin/env python3
"""
Bring-up test: DS3218 pan servo.

Performs a full sweep 1° → 360° then back to 1° using the wrap-safe
path planner.  Also tests the critical 350° → 10° wrap scenario to
confirm backward traversal is used.

Exits 0 on success, 1 on any hardware or logic failure.

Usage:
    python scripts/test_servo.py [--speed 60]
"""

import sys
import time
import argparse
import logging
import subprocess

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))


def check_prerequisites() -> bool:
    """Print diagnostic info and return False if setup looks wrong."""
    ok = True

    # Check I2C devices
    print("\n── I²C bus scan ──")
    try:
        result = subprocess.run(
            ["i2cdetect", "-y", "1"], capture_output=True, text=True, timeout=5
        )
        print(result.stdout)
        if "40" not in result.stdout:
            print("WARNING: PCA9685 (0x40) not found on I²C bus 1!")
            ok = False
        else:
            print("✓ PCA9685 detected at 0x40")
    except FileNotFoundError:
        print("  i2cdetect not available (install i2c-tools to enable scan)")
    except Exception as exc:
        print(f"  I²C scan failed: {exc}")

    print("\n── Python library check ──")
    try:
        import smbus2  # noqa: F401
        print("✓ smbus2 is installed (used for PCA9685 direct I²C)")
    except ImportError:
        print("ERROR: smbus2 is NOT installed. Run: pip install smbus2")
        ok = False

    print()
    return ok


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=60.0, help="deg/sec")
    parser.add_argument("--skip-prereq", action="store_true", help="Skip prerequisite checks")
    args = parser.parse_args()

    print(f"Servo bring-up test — channel=15, speed={args.speed}°/s")

    if not args.skip_prereq:
        if not check_prerequisites():
            print("FAIL: Prerequisites not met. Fix the issues above and retry.")
            return 1

    from src.motion.servo_controller import ServoController, ServoConfig
    cfg = ServoConfig(speed_deg_per_sec=args.speed)
    ctrl = ServoController(config=cfg)

    if not ctrl.hardware_ready:
        print(
            "\nFAIL: ServoController is in simulation mode — hardware not initialised.\n"
            "  Check that:\n"
            "    1. The SparkFun Pi Servo pHAT is connected and I²C is enabled\n"
            "       (sudo raspi-config → Interface Options → I2C → Enable)\n"
            "    2. adafruit-circuitpython-servokit is installed\n"
            "    3. The PCA9685 is visible at 0x40 on I²C bus 1\n"
            "       (run: i2cdetect -y 1)\n"
            "    4. The pan servo is plugged into channel 15 of the pHAT\n"
        )
        return 1

    print("✓ Hardware initialised — servo on channel 15")

    # --- Test 1: full forward sweep ---
    print("\n[1] Full forward sweep: 1° → 360°")
    ctrl.move_to(1.0)
    time.sleep(0.5)
    ctrl.move_to(360.0)
    print(f"    Position after sweep: {ctrl.position:.1f}°")

    # --- Test 2: wrap traversal (the critical safety test) ---
    print("\n[2] Wrap traversal: 350° → 10°  (must go BACKWARD)")
    ctrl.set_immediate(350.0)
    direction = ServoController.plan_direction(350.0, 10.0)
    print(f"    Planned direction: {direction}")
    assert direction == "backward", f"FAIL: expected 'backward', got '{direction}'"
    ctrl.move_to(10.0)
    print(f"    Position after traversal: {ctrl.position:.1f}°  ✓")

    # --- Test 3: centre ---
    print("\n[3] Move to centre: 180°")
    ctrl.move_to(180.0)
    print(f"    Position: {ctrl.position:.1f}°")

    ctrl.stop()
    print("\nServo test PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
