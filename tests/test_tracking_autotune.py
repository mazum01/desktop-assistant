"""Tests for tracking auto-tune helpers."""
import math
import tempfile
from pathlib import Path

import pytest

from src.services.tracking_service import (
    _analyse_response,
    _persist_head_tracking_params,
)


def test_analyse_response_detects_lag():
    """Servo trailing the face by ~0.2s should show positive lag."""
    samples = []
    dt = 0.05
    lag = 0.2
    for i in range(120):
        t = i * dt
        face = 640 + 200 * math.sin(2 * math.pi * 0.3 * t)
        servo = 180 + (200 / 12.8) * math.sin(2 * math.pi * 0.3 * (t - lag))
        samples.append((face, servo, t))
    lag_s, overshoot = _analyse_response(samples)
    assert 0.05 < lag_s < 0.5


def test_analyse_response_empty():
    lag_s, overshoot = _analyse_response([])
    assert isinstance(lag_s, float)
    assert isinstance(overshoot, float)


def test_persist_head_tracking_params_roundtrip(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "head_tracking:\n"
        "  tracking_gain: 0.28\n"
        "  max_speed_deg_s: 120\n"
        "other_section:\n"
        "  foo: bar\n"
    )
    ok = _persist_head_tracking_params(p, {"tracking_gain": 0.41, "max_speed_deg_s": 140})
    assert ok is True
    text = p.read_text()
    assert "tracking_gain: 0.41" in text
    assert "max_speed_deg_s: 140" in text
    assert "other_section" in text  # untouched
    assert "foo: bar" in text
