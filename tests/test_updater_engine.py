"""Deployment transactions using real temporary Git repos and simulated hardware."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.updater.contract import REPOSITORY_URL, load_manifest
from src.updater.engine import Engine, preserve_overrides, render_unit
from src.updater.host import DeploymentError, Host

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          text=True, capture_output=True).stdout.strip()


class FakeHost(Host):
    """Run only Git/file operations; record dependency, build, systemd and reboot."""

    def __init__(self, policy, upstream, manifest):
        super().__init__(policy)
        self.upstream = upstream
        self.calls = []
        self.fail_build = False
        self.fail_health = 0
        self.system_unit_dir = upstream.parent / "system"
        self.system_unit_dir.mkdir()
        self.control_dir = upstream.parent / "control"
        self.control_dir.mkdir()
        self.udev_path = upstream.parent / "udev"
        self.udev_path.write_text("previous rule")
        self.boot_config = upstream.parent / "boot.txt"
        self.boot_config.write_text("\n".join(manifest["boot_lines"]) + "\n")
        self.fan = upstream.parent / "fan"
        self.fan.mkdir()
        for name, value in (("period", "40000"), ("duty_cycle", "40000"), ("enable", "1")):
            (self.fan / name).write_text(value)
        self.unit_list = []
        for name in manifest["units"]:
            destination = self.home / ".config/systemd/user" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(render_unit((ROOT / "services/systemd" / name).read_text(), policy, "user"))
            self.unit_list.append({"name": name, "scope": "user", "path": str(destination), "active": True})

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == "git":
            args = [str(self.upstream) if arg == REPOSITORY_URL else arg for arg in args]
            return subprocess.run(args, check=True, text=True, capture_output=True).stdout
        if args[0] == "bash":
            if self.fail_build:
                self.fail_build = False
                raise DeploymentError("Injected native build failure")
            source = Path(args[1]).parent.parent / "src/perception/scrfd_decode"
            source.mkdir(parents=True, exist_ok=True)
            (source / "scrfd_decode_cpp.so").write_bytes(b"simulated native build")
        return ""

    def write_user_file(self, destination, data, mode=0o644):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    def systemctl(self, scope, *args):
        self.calls.append(["systemctl", scope, *args])
        return "active\n"

    def units(self, names):
        return [dict(unit) for unit in self.unit_list if unit["name"] in names]

    def fan_safe(self):
        return self.fan

    def platform(self):
        return "aarch64:trixie"

    def readiness(self, units, revision, version, manifest, started_after):
        if self.fail_health:
            self.fail_health -= 1
            raise DeploymentError("Injected readiness failure")
        assert self.git("rev-parse", "HEAD") == revision
        assert (self.checkout / "VERSION").read_text().strip() == version
        assert len([unit for unit in units if unit["active"]]) >= 3


@pytest.fixture
def device(tmp_path):
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    git(upstream, "init", "-b", "main")
    git(upstream, "config", "user.email", "test@example.invalid")
    git(upstream, "config", "user.name", "Updater test")
    for directory in ("config", "services"):
        shutil.copytree(ROOT / directory, upstream / directory)
    # Never copy device-local personal files or ignored large models into fixtures.
    shutil.rmtree(upstream / "config")
    (upstream / "config").mkdir()
    for name in ("assistant.yaml", "thermal.yaml", "deployment.json", "updater.json", "stereo_cal.npz"):
        shutil.copy2(ROOT / "config" / name, upstream / "config" / name)
    (upstream / "scripts").mkdir()
    (upstream / "scripts/build_scrfd_decode.sh").write_text("#!/bin/bash\nexit 0\n")
    (upstream / "requirements.txt").write_text("")
    (upstream / "src").mkdir()
    (upstream / "src/__init__.py").touch()
    (upstream / "VERSION").write_text("1.63.1\n")
    manifest = json.loads((upstream / "config/deployment.json").read_text())
    manifest.update(version="1.63.1", assets=[], minimum_free_bytes=1)
    (upstream / "config/deployment.json").write_text(json.dumps(manifest))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "baseline")
    old = git(upstream, "rev-parse", "HEAD")
    (upstream / "VERSION").write_text("1.64.0\n")
    manifest["version"] = "1.64.0"
    (upstream / "config/deployment.json").write_text(json.dumps(manifest))
    git(upstream, "add", ".")
    git(upstream, "commit", "-m", "release")
    git(upstream, "tag", "v1.64.0")
    revision = git(upstream, "rev-parse", "HEAD")
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "clone", str(upstream), str(checkout)], check=True, capture_output=True)
    git(checkout, "checkout", "--detach", old)
    git(checkout, "branch", "-f", "main", old)
    git(checkout, "checkout", "main")
    (checkout / ".git/info/exclude").write_text("__pycache__/\n*.so\nconfig/piper/*.onnx\n")
    (checkout / "config/piper").mkdir()
    (checkout / "config/piper/en_US-lessac-medium.onnx").write_bytes(b"existing voice")
    policy = {
        "checkout": str(checkout), "user": "tester", "uid": os.getuid(), "gid": os.getgid(),
        "group": "tester", "home": str(tmp_path / "home"), "web_url": "http://localhost:8080",
        "default_scope": "user",
    }
    host = FakeHost(policy, upstream, manifest)
    engine = Engine(policy, tmp_path / "state", host)
    release = {"version": "1.64.0", "tag": "v1.64.0", "revision": revision}
    return engine, host, release, old


def test_release_contract_matches_repo():
    assert load_manifest(ROOT)["version"] == (ROOT / "VERSION").read_text().strip()


def test_installs_and_preserves_local_config(device):
    import yaml
    engine, host, release, old = device
    config = engine.checkout / "config/assistant.yaml"
    local = yaml.safe_load(config.read_text())
    local["display"]["ble_address"] = "AA:BB:CC:DD:EE:FF"
    config.write_text(yaml.safe_dump(local))
    calibration = (engine.checkout / "config/stereo_cal.npz").read_bytes()
    engine.install(release)
    assert engine.state["phase"] == "success"
    assert host.git("rev-parse", "HEAD") == release["revision"]
    assert host.git("branch", "--show-current") == "main"
    assert yaml.safe_load(config.read_text())["display"]["ble_address"] == "AA:BB:CC:DD:EE:FF"
    assert (engine.checkout / "config/stereo_cal.npz").read_bytes() == calibration
    assert not engine.state.get("transaction")
    assert any(call[0] == "apt-get" for call in host.calls)


def test_blocks_edited_source(device):
    engine, host, release, old = device
    (engine.checkout / "src/__init__.py").write_text("# local source edit")
    with pytest.raises(DeploymentError, match="source"):
        engine.install(release)
    assert host.git("rev-parse", "HEAD") == old
    assert not any(call[0] == "apt-get" for call in host.calls)


def test_build_failure_recovers_and_quarantines(device):
    engine, host, release, old = device
    host.fail_build = True
    with pytest.raises(DeploymentError, match="build"):
        engine.install(release)
    assert engine.state["phase"] == "rolled_back"
    assert engine.state["failed_revision"] == release["revision"]
    assert host.git("rev-parse", "HEAD") == old
    with pytest.raises(DeploymentError, match="retry"):
        engine.install(release)
    engine.install(release, retry=True)
    assert engine.state["phase"] == "success"


def test_new_release_health_failure_restores_old_revision(device):
    engine, host, release, old = device
    host.fail_health = 1
    with pytest.raises(DeploymentError, match="readiness"):
        engine.install(release)
    assert engine.state["phase"] == "rolled_back"
    assert host.git("rev-parse", "HEAD") == old
    assert host.git("branch", "--show-current") == "main"


def test_failed_recovery_is_not_success(device):
    engine, host, release, old = device
    host.fail_health = 2
    with pytest.raises(DeploymentError, match="Manual repair"):
        engine.install(release)
    assert engine.state["phase"] == "manual_repair"
    assert engine.state["transaction"]


def test_reboot_continuation(device):
    engine, host, release, old = device
    path = host.upstream / "config/deployment.json"
    manifest = json.loads(path.read_text())
    manifest["reboot"] = "required"
    path.write_text(json.dumps(manifest))
    git(host.upstream, "add", ".")
    git(host.upstream, "commit", "-m", "require reboot")
    git(host.upstream, "tag", "-f", release["tag"])
    release["revision"] = git(host.upstream, "rev-parse", "HEAD")
    engine.install(release)
    assert engine.state["phase"] == "pending_reboot"
    assert engine.state["pending_reboot"]
    resumed = Engine(engine.policy, engine.root, host)
    with patch("src.updater.engine.boot_id", return_value="a-different-boot"):
        resumed.resume()
    assert resumed.state["phase"] == "success"
    assert not resumed.state["pending_reboot"]
    assert sum(call == ["systemctl", "reboot"] for call in host.calls) == 1


def test_moved_tag_rejected(device):
    engine, host, release, old = device
    release["revision"] = "0" * 40
    with pytest.raises(DeploymentError, match="moved"):
        engine.stage(release)
    assert host.git("rev-parse", "HEAD") == old


def test_detached_install_supported(device):
    engine, host, release, old = device
    git(engine.checkout, "checkout", "--detach", old)
    engine.install(release)
    assert engine.state["phase"] == "success"
    assert host.git("branch", "--show-current") == ""


def test_stable_selection_and_no_downgrade(device):
    engine, host, release, old = device
    with patch.object(engine, "github", side_effect=[
        {"tag_name": "v1.64.0", "draft": False, "prerelease": False, "html_url": "https://github.com/example"},
        {"sha": release["revision"]},
    ]):
        assert engine.check() == release
    with patch.object(engine, "github", return_value={"tag_name": "v1.0.0"}):
        assert engine.check() is None


def test_service_overrides_preserved():
    previous = "[Service]\nExecStart=/usr/bin/python3 -m src.assistant.core_main\nEnvironment=A=old\nRestartSec=3\n"
    local = previous.replace("A=old", "A=device")
    incoming = previous.replace("RestartSec=3", "RestartSec=5")
    merged = preserve_overrides(previous, local, incoming)
    assert "A=device" in merged and "RestartSec=5" in merged
    with pytest.raises(DeploymentError, match="ExecStart"):
        preserve_overrides(previous, local.replace("core_main", "another"), incoming)


def test_asset_checksum_failure_blocks_activation(device):
    engine, host, release, old = device
    stage = engine.root / "asset-stage"
    (stage / "config").mkdir(parents=True)
    asset = {"path": "config/piper/en_US-lessac-medium.onnx",
             "url": "https://huggingface.co/example", "sha256": "0" * 64, "when": "piper"}
    import io
    with patch("src.updater.engine.urllib.request.urlopen", return_value=io.BytesIO(b"corrupt")):
        with pytest.raises(DeploymentError, match="checksum"):
            engine.stage_assets(stage, {"assets": [asset]})
    assert not (stage / asset["path"]).exists()
    assert host.git("rev-parse", "HEAD") == old


def test_unsupported_platform_blocks_before_global_changes(device):
    engine, host, release, old = device
    with patch.object(host, "platform", return_value="x86_64:trixie"):
        with pytest.raises(DeploymentError, match="platform"):
            engine.install(release)
    assert not any(call[0] == "apt-get" for call in host.calls)


def test_interrupted_install_recovers_from_persisted_state(device):
    engine, host, release, old = device
    with patch.object(engine, "deploy"):
        engine.install(release)
    assert engine.state["transaction"]
    resumed = Engine(engine.policy, engine.root, host)
    resumed.resume()
    assert resumed.state["phase"] == "rolled_back"
    assert host.git("rev-parse", "HEAD") == old


def test_no_repeat_reboot_without_new_boot(device):
    engine, host, release, old = device
    with patch.object(engine, "deploy"):
        engine.install(release)
    engine.state["phase"] = "pending_reboot"
    engine.save()
    with pytest.raises(DeploymentError, match="still required"):
        engine.resume()
    assert not any(call == ["systemctl", "reboot"] for call in host.calls)


def test_schedule_does_not_install_outside_window(device):
    engine, host, release, old = device
    engine.state["release"] = release
    engine.state["check_time"] = __import__("time").time()
    with patch("src.updater.engine.in_window", return_value=False), patch.object(engine, "install") as install:
        engine.scheduled()
    install.assert_not_called()


def test_failed_release_quarantine_applies_to_schedule(device):
    engine, host, release, old = device
    engine.state["release"] = release
    engine.state["failed_revision"] = release["revision"]
    engine.state["check_time"] = __import__("time").time()
    with patch("src.updater.engine.in_window", return_value=True), patch.object(engine, "install") as install:
        engine.scheduled()
    install.assert_not_called()
