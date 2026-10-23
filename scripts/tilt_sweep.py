#!/usr/bin/env python3
"""Standalone tilt servo sweep: 80 -> 100 -> 80 degrees, five times, then 90.

Independent of the VERA code base: it drives the PCA9685 servo driver directly
through adafruit_servokit and imports nothing from ``src``.

Angles are raw servo shaft angles (0-180). Use --reversed to mirror them
(shaft = 180 - angle) when the servo is mounted the other way round.

Stop the VERA core first (or disable face tracking); otherwise it will keep
writing to the same servo channel and fight this script.
"""

from __future__ import annotations

import argparse
import math
import sys
import time

STEP_S = 0.02
HARD_MIN_DEG = 70.0
HARD_MAX_DEG = 110.0


def sweep_plan(low: float, high: float, cycles: int, neutral: float) -> list[float]:
    """Ordered targets: start at low, go high and back `cycles` times, then neutral."""
    return [low] + [high, low] * cycles + [neutral]


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--channel", type=int, default=0, help="PCA9685 channel (default 0)")
    parser.add_argument("--address", type=lambda v: int(v, 0), default=0x40,
                        help="PCA9685 I2C address (default 0x40)")
    parser.add_argument("--low", type=float, default=80.0, help="low angle (default 80)")
    parser.add_argument("--high", type=float, default=100.0, help="high angle (default 100)")
    parser.add_argument("--neutral", type=float, default=90.0, help="final angle (default 90)")
    parser.add_argument("--cycles", type=int, default=5, help="sweeps (default 5)")
    parser.add_argument("--speed", type=float, default=40.0, help="deg/s (default 40)")
    parser.add_argument("--pause", type=float, default=0.3, help="pause at each end, s")
    parser.add_argument("--pulse-min", type=int, default=500, help="min pulse, us")
    parser.add_argument("--pulse-max", type=int, default=2500, help="max pulse, us")
    parser.add_argument("--reversed", action="store_true",
                        help="mirror angles for a reversed mount (shaft = 180 - angle)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the moves without touching hardware")
    args = parser.parse_args(argv)

    angles = (args.low, args.high, args.neutral)
    if not all(math.isfinite(a) and HARD_MIN_DEG <= a <= HARD_MAX_DEG for a in angles):
        parser.error(f"angles must be within {HARD_MIN_DEG:g}-{HARD_MAX_DEG:g} degrees")
    if not args.low < args.high:
        parser.error("--low must be less than --high")
    if args.cycles < 1:
        parser.error("--cycles must be at least 1")
    if not math.isfinite(args.speed) or not 0 < args.speed <= 180:
        parser.error("--speed must be in (0, 180] deg/s")
    if not 0 <= args.channel <= 15:
        parser.error("--channel must be 0-15")
    if not 0 < args.pulse_min < args.pulse_max <= 2500:
        parser.error("pulse range must satisfy 0 < min < max <= 2500")
    if args.pause < 0:
        parser.error("--pause must not be negative")
    return args


class TiltServo:
    def __init__(self, args: argparse.Namespace, start: float) -> None:
        self._args = args
        self.position = start
        self._servo = None
        if args.dry_run:
            return
        try:
            from adafruit_servokit import ServoKit
            kit = ServoKit(channels=16, address=args.address, frequency=50)
        except Exception as exc:
            raise SystemExit(f"Servo hardware unavailable: {type(exc).__name__}: {exc}")
        self._servo = kit.servo[args.channel]
        self._servo.set_pulse_width_range(args.pulse_min, args.pulse_max)

    def _write(self, angle: float) -> None:
        shaft = 180.0 - angle if self._args.reversed else angle
        if self._servo is not None:
            self._servo.angle = shaft

    def move_to(self, target: float) -> None:
        start = self.position
        distance = target - start
        steps = max(1, math.ceil(abs(distance) / (self._args.speed * STEP_S)))
        for step in range(1, steps + 1):
            self._write(start + distance * step / steps)
            if self._servo is not None:
                time.sleep(STEP_S)
        self.position = target


def main(argv=None) -> int:
    args = parse_args(argv)
    plan = sweep_plan(args.low, args.high, args.cycles, args.neutral)
    mount = "reversed" if args.reversed else "direct"
    print(f"Tilt channel {args.channel}, {mount} mount: {args.low:g} -> {args.high:g} -> "
          f"{args.low:g} x{args.cycles}, then {args.neutral:g} at {args.speed:g} deg/s")
    if args.dry_run:
        print("Dry run: " + " ".join(f"{a:g}" for a in plan))

    # The servo cannot report its position, so the first move assumes it starts
    # at neutral; any larger initial offset is a single quick step to the first target.
    tilt = TiltServo(args, start=args.neutral)
    completed = 0
    try:
        for index, target in enumerate(plan[:-1]):
            tilt.move_to(target)
            if not args.dry_run:
                time.sleep(args.pause)
            if index and index % 2 == 0:
                completed += 1
                print(f"Cycle {completed}/{args.cycles} done")
    except KeyboardInterrupt:
        print("Interrupted; returning to neutral")
    finally:
        tilt.move_to(args.neutral)
    print(f"Neutral at {args.neutral:g} degrees")
    return 0


if __name__ == "__main__":
    sys.exit(main())
