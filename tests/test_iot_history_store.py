"""Tests for IoTHistoryStore."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from src.iot.history_store import IoTHistoryStore, MAX_POINTS


def _store(tmp_path) -> IoTHistoryStore:
    return IoTHistoryStore(path=tmp_path / "iot_history.json")


def test_empty_on_new_store(tmp_path):
    s = _store(tmp_path)
    assert s.get("nest_thermostat") == []


def test_push_single_value(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [42.0])
    assert s.get("device_a") == [42.0]


def test_push_accumulates(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [10.0])
    s.push("device_a", [20.0])
    s.push("device_a", [30.0])
    assert s.get("device_a") == [10.0, 20.0, 30.0]


def test_push_seeds_full_deque_on_empty(tmp_path):
    """First push with many values seeds the whole buffer."""
    s = _store(tmp_path)
    values = [float(i) for i in range(40)]
    s.push("device_a", values)
    assert s.get("device_a") == values


def test_push_only_appends_latest_when_non_empty(tmp_path):
    """Subsequent pushes of multi-value lists only append the last element."""
    s = _store(tmp_path)
    s.push("device_a", [1.0])     # seed with single point
    s.push("device_a", [2.0, 3.0, 4.0])   # only last (4.0) should be appended
    assert s.get("device_a") == [1.0, 4.0]


def test_trims_to_max_points(tmp_path):
    s = _store(tmp_path)
    # Seed with MAX_POINTS values
    seed = [float(i) for i in range(MAX_POINTS)]
    s.push("device_a", seed)
    # Push one more
    s.push("device_a", [999.0])
    buf = s.get("device_a")
    assert len(buf) == MAX_POINTS
    assert buf[-1] == 999.0
    assert buf[0] == 1.0   # first element trimmed


def test_persist_and_reload(tmp_path):
    path = tmp_path / "iot_history.json"
    s1 = IoTHistoryStore(path=path)
    s1.push("nest_thermostat", [50.0, 60.0, 70.0])
    s1.save()

    s2 = IoTHistoryStore(path=path)
    assert s2.get("nest_thermostat") == [50.0, 60.0, 70.0]


def test_save_is_noop_when_not_dirty(tmp_path):
    path = tmp_path / "iot_history.json"
    s = IoTHistoryStore(path=path)
    s.save()   # no data, dirty=False — should not raise or create file
    assert not path.exists()


def test_get_returns_copy(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [1.0, 2.0])
    copy = s.get("device_a")
    copy.append(999.0)
    assert s.get("device_a") == [1.0, 2.0]   # original unchanged


def test_multiple_devices_independent(tmp_path):
    s = _store(tmp_path)
    s.push("dev_a", [10.0])
    s.push("dev_b", [20.0])
    assert s.get("dev_a") == [10.0]
    assert s.get("dev_b") == [20.0]
