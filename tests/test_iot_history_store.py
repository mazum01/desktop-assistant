"""Tests for IoTHistoryStore."""

from __future__ import annotations

import json

from src.iot.history_store import IoTHistoryStore, MAX_POINTS


def _store(tmp_path) -> IoTHistoryStore:
    return IoTHistoryStore(path=tmp_path / "iot_history.json")


def test_empty_on_new_store(tmp_path):
    s = _store(tmp_path)
    assert s.get("nest_thermostat") == []


def test_push_single_value(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [42.0], now_ts=1000.0)
    assert s.get("device_a") == [42.0]


def test_push_accumulates_with_interval(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [10.0], now_ts=1000.0, sample_interval_s=5)
    s.push("device_a", [20.0], now_ts=1002.0, sample_interval_s=5)  # too soon
    s.push("device_a", [30.0], now_ts=1006.0, sample_interval_s=5)
    assert s.get("device_a") == [20.0, 30.0]


def test_push_seeds_full_series_on_empty(tmp_path):
    s = _store(tmp_path)
    values = [float(i) for i in range(40)]
    s.push("device_a", values, now_ts=2000.0, sample_interval_s=10)
    assert s.get("device_a") == values


def test_trims_to_horizon(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [1.0], now_ts=0.0)
    s.push("device_a", [2.0], now_ts=120.0)
    s.push("device_a", [3.0], now_ts=240.0)
    assert s.get("device_a", horizon_s=180, now_ts=240.0) == [2.0, 3.0]


def test_trims_to_max_points(tmp_path):
    s = _store(tmp_path)
    vals = [float(i) for i in range(MAX_POINTS + 5)]
    s.push("device_a", vals, now_ts=1000.0)
    got = s.get("device_a")
    assert len(got) == MAX_POINTS
    assert got[0] == 5.0
    assert got[-1] == float(MAX_POINTS + 4)


def test_persist_and_reload_new_format(tmp_path):
    path = tmp_path / "iot_history.json"
    s1 = IoTHistoryStore(path=path)
    s1.push("nest_thermostat", [50.0, 60.0, 70.0], now_ts=5000.0, sample_interval_s=60)
    s1.save()

    raw = json.loads(path.read_text())
    assert isinstance(raw["nest_thermostat"][0], list)
    assert len(raw["nest_thermostat"][0]) == 2

    s2 = IoTHistoryStore(path=path)
    assert s2.get("nest_thermostat") == [50.0, 60.0, 70.0]


def test_load_legacy_list_format(tmp_path):
    path = tmp_path / "iot_history.json"
    path.write_text(json.dumps({"legacy_dev": [1, 2, 3]}))
    s = IoTHistoryStore(path=path)
    assert s.get("legacy_dev") == [1.0, 2.0, 3.0]


def test_save_is_noop_when_not_dirty(tmp_path):
    path = tmp_path / "iot_history.json"
    s = IoTHistoryStore(path=path)
    s.save()
    assert not path.exists()


def test_get_returns_copy(tmp_path):
    s = _store(tmp_path)
    s.push("device_a", [1.0, 2.0], now_ts=1000.0)
    copy = s.get("device_a")
    copy.append(999.0)
    assert s.get("device_a") == [1.0, 2.0]


def test_multiple_devices_independent(tmp_path):
    s = _store(tmp_path)
    s.push("dev_a", [10.0], now_ts=1000.0)
    s.push("dev_b", [20.0], now_ts=1000.0)
    assert s.get("dev_a") == [10.0]
    assert s.get("dev_b") == [20.0]
