#!/usr/bin/env python3
"""
Bring-up test: TMP117 temperature sensor.

Reads 5 samples at 1 s intervals and prints them.
Exits 0 on success, 1 on failure.

Usage:
    python scripts/test_tmp117.py
"""

import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.thermal.tmp117 import TMP117, TMP117Error


def main() -> int:
    print("TMP117 bring-up test")
    print("Connecting to TMP117 on I²C bus 1, address 0x48 ...")

    try:
        sensor = TMP117()
    except TMP117Error as e:
        print(f"ERROR: {e}")
        return 1

    print("Device verified. Reading 5 samples:\n")
    try:
        for i in range(1, 6):
            temp_c = sensor.read_temperature_c()
            temp_f = sensor.read_temperature_f()
            print(f"  Sample {i}: {temp_c:.4f} °C  /  {temp_f:.4f} °F")
            if i < 5:
                time.sleep(1)
    except TMP117Error as e:
        print(f"ERROR during read: {e}")
        return 1
    finally:
        sensor.close()

    print("\nTMP117 test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
