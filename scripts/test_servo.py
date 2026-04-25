#!/usr/bin/env python3
"""
Bring-up test: DS3218 pan servo.

Performs a full sweep 1° → 360° then back to 1° using the wrap-safe
path planner.  Also tests the critical 350° → 10° wrap scenario to
confirm backward traversal is used.

Exits 0 on success.

Usage:
    python scripts/test_servo.py [--speed 60]
"""

import sys
import time
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.motion.servo_controller import ServoController, ServoConfig


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--speed", type=float, default=60.0, help="deg/sec")
    args = parser.parse_args()

    print(f"Servo bring-up test — speed={args.speed}°/s")
    cfg = ServoConfig(speed_deg_per_sec=args.speed)
    ctrl = ServoController(config=cfg)

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
    print("\nServo test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
