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
import argparse

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.thermal.tmp117 import TMP117, TMP117Error


def _parse_address(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("address must be an integer (for example 0x48)") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Probe a TMP117 directly over I²C without starting VERA services."
    )
    parser.add_argument("--bus", type=int, default=1, help="I²C bus number (default: 1)")
    parser.add_argument(
        "--address", type=_parse_address, default=0x48,
        help="I²C address (decimal or hex; default: 0x48)",
    )
    args = parser.parse_args()

    print("TMP117 bring-up test")
    print(f"Connecting to TMP117 on I²C bus {args.bus}, address 0x{args.address:02X} ...")

    try:
        sensor = TMP117(bus=args.bus, address=args.address)
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
