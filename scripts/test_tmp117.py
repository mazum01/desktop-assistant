#!/usr/bin/env python3
"""Bring-up test for the supported TMP117 and TMP102 temperature sensors.

Reads 5 samples at 1 s intervals and prints them.
Exits 0 on success, 1 on failure.

Usage:
    python scripts/test_tmp117.py [--bus 1] [--address 0x48]
"""

import argparse
import sys
import time

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from src.thermal.temperature_sensor import (
    TemperatureSensorProbeError,
    probe_temperature_sensor,
)
from src.thermal.tmp117 import TMP117Error


def _parse_address(value: str) -> int:
    try:
        return int(value, 0)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("address must be an integer (for example 0x48)") from exc


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe a TMP117 or TMP102 directly over I²C without starting VERA services."
        )
    )
    parser.add_argument("--bus", type=int, default=1, help="I²C bus number (default: 1)")
    parser.add_argument(
        "--address", type=_parse_address, default=0x48,
        help="I²C address (decimal or hex; default: 0x48)",
    )
    args = parser.parse_args()

    print("Temperature sensor bring-up test")
    print(f"Probing I²C bus {args.bus}, address 0x{args.address:02X} ...")

    try:
        sensor = probe_temperature_sensor(bus=args.bus, address=args.address)
    except (TemperatureSensorProbeError, TMP117Error) as e:
        print(f"ERROR: {e}")
        return 1

    print(f"Detected {sensor.sensor_type}. Reading 5 samples:\n")
    try:
        for i in range(1, 6):
            temp_c = sensor.read_temperature_c()
            temp_f = sensor.read_temperature_f()
            print(f"  Sample {i}: {temp_c:.4f} °C  /  {temp_f:.4f} °F")
            if i < 5:
                time.sleep(1)
    except (TemperatureSensorProbeError, TMP117Error) as e:
        print(f"ERROR during read: {e}")
        return 1
    finally:
        sensor.close()

    print("\nTemperature sensor test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
