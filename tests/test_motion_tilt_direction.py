"""Tilt convention: 90 is level, above 90 looks up; mount reversal stays internal."""

from unittest.mock import MagicMock

import pytest

from src.core.bus import MessageBus
from src.motion.servo_controller import ServoConfig, ServoController
from src.services.motion_service import MotionService


def make_service(reversed_mount):
    kit = MagicMock(servo=[MagicMock(angle=None) for _ in range(16)])
    pan = ServoController(ServoConfig(
        channel=1, range_deg=180, soft_min_deg=70, soft_max_deg=110,
    ), kit=kit, initialize_hardware=False)
    svc = MotionService(
        bus=MessageBus(), controller=pan, soft_min_deg=70, soft_max_deg=110,
        pan_channel=1, tilt_channel=0, range_deg=180,
        tilt_enabled=True, tilt_reversed=reversed_mount,
    )
    svc.on_start()
    return svc, kit


@pytest.mark.parametrize("reversed_mount,shaft", [(False, 100), (True, 80)])
def test_logical_up_angle_maps_to_mount_direction(reversed_mount, shaft):
    svc, kit = make_service(reversed_mount)
    try:
        svc._tilt_controller._write(100)
        assert kit.servo[0].angle == shaft
        assert svc._tilt_controller.position == 100
        assert svc.get_status()["tilt_reversed"] is reversed_mount
    finally:
        svc.on_stop()


def test_level_tilt_is_unchanged_by_mount_reversal():
    svc, kit = make_service(True)
    try:
        svc._tilt_controller._write(90)
        assert kit.servo[0].angle == 90
    finally:
        svc.on_stop()
