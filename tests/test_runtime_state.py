"""Tests for src.core.runtime_state."""

import tempfile
from pathlib import Path

import pytest

from src.core.runtime_state import load, save


def test_load_missing_file(tmp_path):
    """Missing file returns empty dict without error."""
    result = load(path=tmp_path / "nonexistent.yaml")
    assert result == {}


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "runtime_state.yaml"
    state = {
        "servo": {"enabled": False},
        "head_tracking": {
            "face_tracking_enabled": True,
            "random_motion_enabled": False,
        },
    }
    save(state, path=path)
    loaded = load(path=path)
    assert loaded == state


def test_save_is_atomic(tmp_path):
    """Temp file is cleaned up; only the final file remains."""
    path = tmp_path / "runtime_state.yaml"
    save({"servo": {"enabled": True}}, path=path)
    tmp = path.with_suffix(".yaml.tmp")
    assert path.exists()
    assert not tmp.exists()


def test_load_corrupt_file(tmp_path):
    """Corrupt YAML returns empty dict rather than raising."""
    path = tmp_path / "runtime_state.yaml"
    path.write_text("{{{{not valid yaml")
    result = load(path=path)
    assert result == {}


def test_load_non_dict_yaml(tmp_path):
    """A YAML file that parses to a list returns empty dict."""
    path = tmp_path / "runtime_state.yaml"
    path.write_text("- item1\n- item2\n")
    result = load(path=path)
    assert result == {}


def test_overlay_semantics():
    """Demonstrate that runtime state keys override config defaults."""
    import yaml

    cfg = {"servo": {"enabled": True}, "head_tracking": {"face_tracking_enabled": True}}
    rt = {"servo": {"enabled": False}}

    servo_enabled = rt.get("servo", {}).get(
        "enabled", cfg.get("servo", {}).get("enabled", True)
    )
    assert servo_enabled is False

    face_tracking = rt.get("head_tracking", {}).get(
        "face_tracking_enabled",
        cfg.get("head_tracking", {}).get("face_tracking_enabled", True),
    )
    assert face_tracking is True
