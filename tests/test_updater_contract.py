"""Release metadata and scheduling tests."""

import datetime as dt
import subprocess
from unittest.mock import Mock

import pytest

from src.updater.contract import in_window, merge_defaults, relative_path, settings, version_tuple


@pytest.mark.parametrize("value", ["v1.0.0", "1.2.3-rc1", "01.2.3", "", "1.2"])
def test_invalid_version(value):
    with pytest.raises(ValueError):
        version_tuple(value)


def test_versions_are_numeric():
    assert version_tuple("1.64.0") > version_tuple("1.9.9")


@pytest.mark.parametrize("start,end,hour,expected", [
    ("02:00", "04:00", 2, True), ("02:00", "04:00", 4, False),
    ("23:00", "02:00", 0, True), ("23:00", "02:00", 3, False),
])
def test_window(start, end, hour, expected):
    assert in_window({"start": start, "end": end}, dt.datetime(2026, 10, 13, hour)) is expected


def test_settings_validation():
    assert settings({"enabled": True, "start": "02:00", "end": "04:00"})["enabled"]
    for invalid in [
        {"enabled": 1, "start": "02:00", "end": "04:00"},
        {"enabled": True, "start": "25:00", "end": "04:00"},
        {"enabled": True, "start": "02:00", "end": "02:00"},
    ]:
        with pytest.raises(ValueError):
            settings(invalid)


def test_three_way_defaults():
    old = {"camera": {"address": "original", "fps": 10}}
    new = {"camera": {"address": "new", "fps": 20, "enabled": True}}
    local = {"camera": {"address": "device-local", "fps": 10}}
    assert merge_defaults(old, new, local) == {
        "camera": {"address": "device-local", "fps": 20, "enabled": True}
    }


def test_incompatible_config_requires_migration():
    with pytest.raises(ValueError, match="Removed"):
        merge_defaults({"old": 1}, {}, {"old": 2})
    with pytest.raises(ValueError, match="type"):
        merge_defaults({"setting": 1}, {"setting": {}}, {"setting": 1})


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside", ".git/config", "config/../../etc", "config\\bad"])
def test_unsafe_paths(path):
    with pytest.raises(ValueError):
        relative_path(path)


@pytest.mark.parametrize("suffix", ["", "-rc.1", "-rc.12"])
def test_release_validator_accepts_matching_stable_or_candidate_tag(monkeypatch, suffix):
    from scripts import validate_release

    version = (validate_release.ROOT / "VERSION").read_text().strip()
    monkeypatch.setattr("sys.argv", ["validate_release", "--tag", "v" + version + suffix])
    monkeypatch.setattr(validate_release.subprocess, "run", Mock(
        return_value=subprocess.CompletedProcess([], 0, stdout=""),
    ))
    validate_release.main()


@pytest.mark.parametrize("suffix", ["-rc.0", "-rc.01", "-rc.x", "-anything"])
def test_release_validator_rejects_invalid_candidate_tag(monkeypatch, suffix):
    from scripts import validate_release

    version = (validate_release.ROOT / "VERSION").read_text().strip()
    monkeypatch.setattr("sys.argv", ["validate_release", "--tag", "v" + version + suffix])
    with pytest.raises(SystemExit):
        validate_release.main()
