"""Fresh runtime readiness and safe system operation tests."""

from unittest.mock import patch

import pytest

from src.updater.host import DeploymentError, Host


def host(tmp_path):
    return Host({"checkout": str(tmp_path), "user": "tester", "uid": 1000, "home": str(tmp_path)})


def test_fan_verification_requires_full_duty(tmp_path):
    for key, value in (("period", "40000"), ("duty_cycle", "40000"), ("enable", "1")):
        (tmp_path / key).write_text(value)
    Host.verify_fan(tmp_path)
    (tmp_path / "enable").write_text("0")
    with pytest.raises(DeploymentError, match="fan protection"):
        Host.verify_fan(tmp_path)


def test_unexpected_command_failure_surfaces(tmp_path):
    with pytest.raises(DeploymentError, match="Command failed"):
        host(tmp_path).run(["/usr/bin/false"])


def test_wrong_loaded_revision_fails_health(tmp_path):
    instance = host(tmp_path)
    units = [{"name": "desktop-assistant-core.service", "scope": "user", "active": True}]
    with patch.object(instance, "systemctl", return_value="active"), \
         patch.object(instance, "rpc", return_value={"revision": "old", "version": "1.0.0"}), \
         patch("src.updater.host.time.sleep"):
        with pytest.raises(DeploymentError, match="different revision"):
            instance.readiness(units, "new", "1.0.0",
                               {"health_timeout_seconds": 0.01, "observation_seconds": 1}, 0)


def test_per_unit_watchdog_scope_overrides_legacy_defaults(monkeypatch):
    from src.watchdog.watchdog import _unit_user_scope

    monkeypatch.setenv("VERA_SYSTEMD_SCOPE_MEDIA", "system")
    assert _unit_user_scope("media", True) is False
    monkeypatch.setenv("VERA_SYSTEMD_SCOPE_THERMAL", "user")
    assert _unit_user_scope("thermal", False) is True
    monkeypatch.setenv("VERA_SYSTEMD_SCOPE_CORE", "invalid")
    with pytest.raises(ValueError, match="scope"):
        _unit_user_scope("core", False)
