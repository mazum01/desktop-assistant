"""Bounded smoothstep execution and calibration command ownership."""

from unittest.mock import MagicMock, patch

import pytest

from src.core.bus import MessageBus
from src.services.motion_service import MotionService


class Servo:
    def __init__(self):
        self.position = 90.0
        self.position_initialized = True
        self.hardware_ready = True

    def _write(self, position):
        self.position = position

    def plan_direction(self, before, after):
        return "forward" if after >= before else "backward"

    def stop(self):
        pass


def test_quintic_profile_bounded_speed_acceleration_and_exact_finish():
    servo = Servo()
    svc = MotionService(controller=servo, soft_min_deg=70, soft_max_deg=110)
    with patch("src.services.motion_service.time.monotonic", return_value=0):
        svc._on_pan_to("", {
            "angle": 92, "move_time_ms": 1000, "profile": "smoothstep",
        })
    positions = []
    for tick in range(51):
        with patch("src.services.motion_service.time.monotonic", return_value=tick * 0.02):
            svc._step_axis(servo, 92, 2)
        positions.append(servo.position)
    velocity = [(b - a) / 0.02 for a, b in zip(positions, positions[1:])]
    accel = [(b - a) / 0.02 for a, b in zip(velocity, velocity[1:])]
    assert max(abs(v) for v in velocity) <= 3.75 + 0.01
    assert max(abs(a) for a in accel) <= 11.56 + 0.01
    assert positions[-1] == 92


def test_calibration_lease_blocks_other_moves_and_stop_clears_profile():
    servo = Servo()
    svc = MotionService(bus=MessageBus(), controller=servo, soft_min_deg=70, soft_max_deg=110)
    svc._on_calibration_lock("", {"enabled": True})
    svc._on_pan_to("", {"angle": 100})
    assert svc._target_angle is None
    svc._on_pan_to("", {
        "angle": 92, "move_time_ms": 1000, "profile": "smoothstep", "source": "calibration",
    })
    assert svc._profiles
    svc._on_calibration_lock("", {"enabled": True})
    assert svc._target_angle == 92
    svc._on_stop_cmd("", {})
    assert not svc._profiles
    assert svc._target_angle is None
    svc._on_calibration_lock("", {"enabled": False})
    svc._on_pan_to("", {"angle": 91})
    assert svc._target_angle == 91
