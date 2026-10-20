#!/usr/bin/env python3
"""Safely exercise the configured pan and tilt servo channels."""

from __future__ import annotations

import argparse
import math
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.ipc_client import IPCClient


def _request(client: IPCClient, request: dict, operation: str) -> dict:
    response = client.call(request)
    if not response.get("ok"):
        raise RuntimeError(f"{operation} failed: {response.get('error', response)}")
    return response


def _last_payload(client: IPCClient, topic: str) -> dict | None:
    payload = _request(client, {"cmd": "last", "topic": topic}, f"read {topic}")["payload"]
    return payload if isinstance(payload, dict) else None


def _wait_for_speech(client: IPCClient, request_id: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        payload = _last_payload(client, "av.spoke")
        if payload and payload.get("request_id") == request_id:
            return
        time.sleep(0.1)
    raise TimeoutError("TTS did not confirm playback; no servo move was issued")


def _announce(client: IPCClient, text: str, timeout: float) -> None:
    request_id = uuid.uuid4().hex
    _request(
        client,
        {
            "cmd": "publish",
            "topic": "av.say",
            "payload": {"text": text, "request_id": request_id},
        },
        "announce move",
    )
    _wait_for_speech(client, request_id, timeout)
    print(f"Announced: {text}")


def _wait_for_position(
    client: IPCClient, axis: str, target: float, timeout: float
) -> None:
    key = "angle" if axis == "pan" else "tilt_angle"
    deadline = time.monotonic() + timeout
    time.sleep(0.15)
    while time.monotonic() < deadline:
        payload = _last_payload(client, "motion.position")
        if payload and payload.get(key) is not None:
            position = float(payload[key])
            if math.isfinite(position) and abs(position - target) <= 0.5:
                return
        time.sleep(0.1)
    raise TimeoutError(f"{axis} did not reach {target:.1f} degrees")


def _move(
    client: IPCClient,
    axis: str,
    channel: int,
    angle: float,
    label: str,
    speech_timeout: float,
    move_timeout: float,
) -> None:
    text = f"Servo test. {axis} channel {channel}: {label}, {angle:.0f} degrees."
    _announce(client, text, speech_timeout)
    _request(
        client,
        {
            "cmd": "publish",
            "topic": f"motion.{axis}_to",
            "payload": {"angle": angle, "move_time_ms": 1000},
        },
        f"move {axis} to {angle:.1f} degrees",
    )
    _wait_for_position(client, axis, angle, move_timeout)
    print(f"Reached {axis} {angle:.1f} degrees on channel {channel}")


def _axis_limits(status: dict, axis: str) -> tuple[float, float]:
    min_key, max_key = (
        ("soft_min_deg", "soft_max_deg")
        if axis == "pan"
        else ("tilt_min_deg", "tilt_max_deg")
    )
    minimum, maximum = float(status[min_key]), float(status[max_key])
    if not (math.isfinite(minimum) and math.isfinite(maximum) and minimum < maximum):
        raise ValueError(f"Invalid {axis} travel limits: {minimum}–{maximum}")
    return minimum, maximum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--speech-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for each spoken announcement to finish",
    )
    parser.add_argument(
        "--move-timeout",
        type=float,
        default=15.0,
        help="seconds to wait for each servo position",
    )
    args = parser.parse_args()
    if args.speech_timeout <= 0 or args.move_timeout <= 0:
        parser.error("timeouts must be positive")

    endpoint = os.environ.get("DA_BUS_REP_ENDPOINT", "ipc:///tmp/desktop-assistant.rep")
    client = IPCClient(endpoint, timeout_ms=5000)
    try:
        status_reply = _request(client, {"cmd": "motion.get_status"}, "read servo status")
        status = status_reply.get("status")
        if not isinstance(status, dict):
            raise RuntimeError("motion.get_status returned no status object")
        if not status.get("hardware_ready"):
            raise RuntimeError("servo hardware is not ready; refusing to move")
        if not status.get("servo_enabled"):
            raise RuntimeError("servo motion is disabled; refusing to move")

        channels = (status.get("pan_channel"), status.get("tilt_channel"))
        if any(type(channel) is not int or not 0 <= channel <= 15 for channel in channels):
            raise RuntimeError(f"invalid configured servo channels: {channels}")
        if channels[0] == channels[1]:
            raise RuntimeError("pan and tilt must use distinct PCA9685 channels")

        axes = []
        for axis, channel in zip(("pan", "tilt"), channels):
            minimum, maximum = _axis_limits(status, axis)
            center = (minimum + maximum) / 2
            low, high = center - 20, center + 20
            if low < minimum or high > maximum:
                raise RuntimeError(
                    f"{axis} limits {minimum:.1f}–{maximum:.1f} do not allow "
                    "a safe +/-20 degree test around center"
                )
            axes.append((axis, channel, center, low, high))

        print(
            "Testing pan channel "
            f"{channels[0]} and tilt channel {channels[1]} "
            "with announced +/-20 degree moves."
        )

        for axis, channel, center, low, high in axes:
            _move(
                client, axis, channel, center, "centering",
                args.speech_timeout, args.move_timeout,
            )
        for axis, channel, center, low, high in axes:
            for angle, label in (
                (low, "minus 20"),
                (center, "returning to center"),
                (high, "plus 20"),
                (center, "returning to center"),
            ):
                _move(
                    client, axis, channel, angle, label,
                    args.speech_timeout, args.move_timeout,
                )
    except (RuntimeError, TimeoutError, ValueError, KeyError) as exc:
        print(f"Servo channel test FAILED: {exc}", file=sys.stderr)
        return 1

    print("Servo channel test PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
