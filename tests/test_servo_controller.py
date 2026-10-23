"""Servo mapping, channel isolation and hardware travel safety."""

from unittest.mock import MagicMock

import pytest

from src.motion.servo_controller import ServoConfig, ServoController


def make_kit():
    return MagicMock(servo=[MagicMock(angle=None) for _ in range(16)])


def make_servo(channel=0, kit=None):
    return ServoController(ServoConfig(
        channel=channel, range_deg=180, soft_min_deg=70, soft_max_deg=110,
    ), kit=kit, initialize_hardware=False)


@pytest.mark.parametrize("angle,expected", [(70, 70), (90, 90), (110, 110), (-50, 70), (400, 110)])
def test_direct_180_degree_mapping(angle, expected):
    kit = make_kit()
    servo = make_servo(kit=kit)
    servo._write(angle)
    assert kit.servo[0].angle == expected
    assert servo.position == expected


@pytest.mark.parametrize("angle,written", [(70, 110), (90, 90), (110, 70), (100, 80)])
def test_reversed_mount_mirrors_shaft_but_keeps_logical_angles(angle, written):
    kit = make_kit()
    servo = ServoController(ServoConfig(
        channel=0, range_deg=180, soft_min_deg=70, soft_max_deg=110, reverse=True,
    ), kit=kit, initialize_hardware=False)
    servo._write(angle)
    assert kit.servo[0].angle == written
    assert servo.position == angle


def test_axis_channels_share_one_kit_without_touching_other_channels():
    kit = make_kit()
    pan = make_servo(0, kit)
    tilt = make_servo(1, kit)
    pan.set_immediate(70)
    tilt.set_immediate(110)
    assert kit.servo[0].angle == 70
    assert kit.servo[1].angle == 110
    assert pan.position == 70
    assert tilt.position == 110


@pytest.mark.parametrize("channel", [-1, 16, 1.5, True])
def test_invalid_channels_rejected(channel):
    with pytest.raises(ValueError):
        ServoConfig(channel=channel)


@pytest.mark.parametrize("angle", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_angle_never_reaches_hardware(angle):
    kit = make_kit()
    servo = make_servo(kit=kit)
    with pytest.raises(ValueError):
        servo._write(angle)
    assert kit.servo[0].angle is None
    assert servo.position == 90


def test_reassign_channel_sets_calibration_without_moving():
    kit = make_kit()
    servo = make_servo(kit=kit)
    servo.set_channel(4)
    kit.servo[4].set_pulse_width_range.assert_called_once_with(500, 2500)
    servo.set_immediate(90)
    assert kit.servo[4].angle == 90


def test_center_command_writes_once_even_before_first_movement():
    from src.services.motion_service import MotionService

    kit = make_kit()
    servo = make_servo(kit=kit)
    service = MotionService(controller=servo)
    assert not servo.position_initialized
    service._step_axis(servo, 90, 90)
    assert kit.servo[0].angle == 90
    assert servo.position_initialized
    servo.relax()
    assert kit.servo[0].angle is None
    service._step_axis(servo, 90, 90)
    assert kit.servo[0].angle == 90


def test_legacy_geared_mapping_and_wrap_are_preserved():
    servo = ServoController(initialize_hardware=False)
    assert servo.logical_to_kit_angle(1) == 0
    assert servo.logical_to_kit_angle(360) == 180
    assert servo.plan_direction(360, 1) == "backward"


@pytest.mark.parametrize("kwargs", [
    {"range_deg": 180, "soft_min_deg": 70, "soft_max_deg": 215},
    {"pulse_min_us": 10, "pulse_max_us": 2950},
    {"soft_min_deg": float("nan")},
])
def test_invalid_calibration_rejected(kwargs):
    with pytest.raises(ValueError):
        ServoConfig(**kwargs)