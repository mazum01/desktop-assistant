#!/usr/bin/env python3
"""
Bring-up test: NF-A6x25 PWM fan.

Ramps from 0% to 100% in 10% steps, holds each for 2 s, then returns to 0%.
Exits 0 on success.

Usage:
    python scripts/test_fan.py [--gpio-pin 13]
"""

import sys
import time
import argparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.thermal.fan import FanController


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gpio-pin", type=int, default=13)
    args = parser.parse_args()

    print(f"Fan bring-up test — GPIO{args.gpio_pin}")
    fan = FanController(gpio_pin=args.gpio_pin)

    steps = list(range(0, 110, 10))
    try:
        for duty in steps:
            fan.set_duty(float(duty))
            print(f"  Duty: {duty:3d}%")
            time.sleep(2)

        print("Returning to 0% ...")
        fan.set_duty(0.0)
        time.sleep(1)
    finally:
        fan.close()

    print("\nFan test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
