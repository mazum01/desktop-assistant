"""Tests for QuietHours gate."""
from __future__ import annotations
import json
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from src.core.quiet_hours import QuietHours


def _qh(enabled=True, start="21:00", end="06:00"):
    return QuietHours(enabled=enabled, start=start, end=end)


def _dt(h, m=0):
    return datetime(2024, 1, 15, h, m, 0)


# ── is_quiet() ────────────────────────────────────────────────────────────

def test_disabled_never_quiet():
    qh = _qh(enabled=False)
    for hour in range(24):
        assert qh.is_quiet(_dt(hour)) is False


def test_overnight_range_inside():
    qh = _qh(start="21:00", end="06:00")
    assert qh.is_quiet(_dt(21)) is True
    assert qh.is_quiet(_dt(23)) is True
    assert qh.is_quiet(_dt(0))  is True
    assert qh.is_quiet(_dt(3))  is True
    assert qh.is_quiet(_dt(5, 59)) is True


def test_overnight_range_outside():
    qh = _qh(start="21:00", end="06:00")
    assert qh.is_quiet(_dt(6))  is False
    assert qh.is_quiet(_dt(12)) is False
    assert qh.is_quiet(_dt(20, 59)) is False


def test_same_day_range():
    qh = _qh(start="08:00", end="18:00")
    assert qh.is_quiet(_dt(8))   is True
    assert qh.is_quiet(_dt(17, 59)) is True
    assert qh.is_quiet(_dt(18)) is False
    assert qh.is_quiet(_dt(7, 59)) is False


def test_exact_boundary_start():
    qh = _qh(start="21:00", end="06:00")
    assert qh.is_quiet(_dt(21, 0)) is True


def test_exact_boundary_end():
    qh = _qh(start="21:00", end="06:00")
    assert qh.is_quiet(_dt(6, 0)) is False


# ── update() + as_dict() ──────────────────────────────────────────────────

def test_update_changes_values():
    qh = _qh(enabled=False, start="09:00", end="17:00")
    qh.update(True, "22:00", "07:00")
    assert qh.enabled is True
    assert qh.start == "22:00"
    assert qh.end == "07:00"


def test_update_invalid_time_raises():
    qh = _qh()
    with pytest.raises(ValueError):
        qh.update(True, "99:00", "06:00")


def test_as_dict():
    qh = _qh(enabled=True, start="21:00", end="06:00")
    d = qh.as_dict()
    assert d == {"enabled": True, "start": "21:00", "end": "06:00"}


# ── from_config() + persistence ───────────────────────────────────────────

def test_from_config_creates_file():
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td)
        qh = QuietHours.from_config(cfg_dir, yaml_defaults={"enabled": True, "start": "22:00", "end": "07:00"})
        assert qh.enabled is True
        assert qh.start == "22:00"
        # File should have been created
        assert (cfg_dir / "quiet_hours.json").exists()


def test_from_config_reads_existing_file():
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td)
        data = {"enabled": False, "start": "08:00", "end": "18:00"}
        (cfg_dir / "quiet_hours.json").write_text(json.dumps(data))
        qh = QuietHours.from_config(cfg_dir)
        assert qh.enabled is False
        assert qh.start == "08:00"
        assert qh.end == "18:00"


def test_update_writes_to_file():
    with tempfile.TemporaryDirectory() as td:
        cfg_dir = Path(td)
        qh = QuietHours.from_config(cfg_dir)
        qh.update(True, "20:00", "05:00")
        saved = json.loads((cfg_dir / "quiet_hours.json").read_text())
        assert saved == {"enabled": True, "start": "20:00", "end": "05:00"}
