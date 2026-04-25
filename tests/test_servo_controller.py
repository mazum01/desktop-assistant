"""
Unit tests for ServoController — no hardware required.

Tests cover:
  - Logical → mechanical angle conversion
  - Wrap-safe direction planning (the core safety rule)
  - Boundary / edge cases
"""

import pytest
from src.motion.servo_controller import ServoController


class TestLogicalToMechanical:
    def test_logical_min_maps_to_mech_zero(self):
        assert ServoController.logical_to_mechanical(1.0) == pytest.approx(0.0)

    def test_logical_max_maps_to_mech_270(self):
        assert ServoController.logical_to_mechanical(360.0) == pytest.approx(270.0, abs=0.1)

    def test_midpoint(self):
        # logical 180.5 → roughly 134.5° mechanical
        mech = ServoController.logical_to_mechanical(180.5)
        assert 130.0 < mech < 140.0

    def test_quarter_point(self):
        mech = ServoController.logical_to_mechanical(90.75)
        assert mech == pytest.approx((90.75 - 1) * (270 / 359), abs=0.01)


class TestPlanDirection:
    """The critical wrap-safety tests."""

    def test_forward_when_target_greater(self):
        assert ServoController.plan_direction(10.0, 350.0) == "forward"

    def test_backward_when_target_less(self):
        # 350 → 10 MUST go backward, not cross the dead zone
        assert ServoController.plan_direction(350.0, 10.0) == "backward"

    def test_forward_when_equal(self):
        assert ServoController.plan_direction(180.0, 180.0) == "forward"

    def test_backward_crossing_wrap_zone(self):
        # Any move from higher to lower must go backward
        assert ServoController.plan_direction(359.0, 1.0) == "backward"

    def test_forward_small_positive(self):
        assert ServoController.plan_direction(1.0, 2.0) == "forward"

    def test_backward_near_boundary(self):
        assert ServoController.plan_direction(360.0, 1.0) == "backward"

    def test_forward_large_range(self):
        assert ServoController.plan_direction(1.0, 360.0) == "forward"

    def test_backward_mid_range(self):
        assert ServoController.plan_direction(200.0, 100.0) == "backward"

    def test_forward_mid_range(self):
        assert ServoController.plan_direction(100.0, 200.0) == "forward"


class TestSimMode:
    """ServoController in simulation mode (no hardware)."""

    def setup_method(self):
        # Will fall back to sim mode since PCA9685 not available in test env
        self.ctrl = ServoController()

    def test_initial_position(self):
        assert self.ctrl.position == pytest.approx(180.0)

    def test_move_to_clamps_to_soft_limits(self):
        self.ctrl.move_to(400.0)   # above max
        assert self.ctrl.position == pytest.approx(360.0)

    def test_move_to_clamps_below_min(self):
        self.ctrl.move_to(-10.0)
        assert self.ctrl.position == pytest.approx(1.0)

    def test_set_immediate(self):
        self.ctrl.set_immediate(270.0)
        assert self.ctrl.position == pytest.approx(270.0)
