"""Persisted, in-place stable-release deployment transactions."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from pathlib import Path

from src.updater.contract import (
    REPOSITORY, REPOSITORY_URL, in_window, load_manifest, merge_defaults,
    settings, version_tuple,
)
from src.updater.host import DeploymentError, Host

log = logging.getLogger(__name__)
ACTIVE_PHASES = {
    "preparing", "installing", "pending_reboot", "verifying", "recovering",
}
LOCAL_CONFIG = ("config/assistant.yaml", "config/thermal.yaml", "config/stereo_cal.npz")


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


class Engine:
    def __init__(self, policy: dict, state_dir: Path, host: Host | None = None):
        self.policy = policy
        self.root = state_dir
        self.host = host or Host(policy)
        self.checkout = Path(policy["checkout"])
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_path = self.root / "state.json"
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {
                "phase": "idle", "message": "No update requested", "history": [],
                "settings": settings(json.loads((self.checkout / "config/updater.json").read_text())),
                "pending_reboot": False, "failed_revision": None,
            }
            self.save()

    def save(self) -> None:
        atomic_json(self.state_path, self.state)

    def phase(self, phase: str, message: str) -> None:
        self.state.update(phase=phase, message=message)
        if phase not in ("blocked", "recovering", "rolled_back", "manual_repair"):
            self.state["error"] = None
        self.state["history"] = (self.state["history"] + [{
            "phase": phase, "message": message, "version": self.state.get("available_version"),
            "ts": time.time(),
        }])[-50:]
        self.save()
        log.info("%s: %s", phase, message)

    def status(self) -> dict:
        result = dict(self.state)
        result["installed_version"] = (self.checkout / "VERSION").read_text().strip()
        # Transaction paths/backup configuration are not part of the public API.
        for key in ("transaction", "release", "failed_revision", "check_time", "previous_backup"):
            result.pop(key, None)
        return result

    def github(self, suffix: str) -> object:
        headers = {"Accept": "application/vnd.github+json", "User-Agent": "VERA-updater"}
        token_path = Path(self.policy.get("token_file", "/etc/desktop-assistant/updater-github-token"))
        if token_path.is_file():
            headers["Authorization"] = "Bearer " + token_path.read_text().strip()
        request = urllib.request.Request(
            f"https://api.github.com/repos/{REPOSITORY}" + ("/" + suffix if suffix else ""),
            headers=headers,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if int(response.headers.get("Content-Length", "0")) > 2_000_000:
                raise DeploymentError("GitHub response exceeds limit")
            data = response.read(2_000_001)
        if len(data) > 2_000_000:
            raise DeploymentError("GitHub response exceeds limit")
        return json.loads(data)

    def check(self) -> dict | None:
        try:
            latest = self.github("releases/latest")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            # Distinguish "no published releases" from inaccessible/private repo.
            self.github("")
            self.state.update(check_time=time.time(), release=None)
            self.phase("up_to_date", "No published stable GitHub release yet")
            return None
        if not isinstance(latest, dict) or latest.get("draft") or latest.get("prerelease"):
            raise DeploymentError("GitHub returned an invalid stable release")
        tag = latest.get("tag_name", "")
        version = tag.removeprefix("v")
        version_tuple(version)
        if tag not in (version, "v" + version):
            raise DeploymentError("Invalid release tag")
        self.state["check_time"] = time.time()
        self.state.update(available_version=version, release_url=latest.get("html_url"))
        if version_tuple(version) <= version_tuple((self.checkout / "VERSION").read_text().strip()):
            self.state["release"] = None
            self.phase("up_to_date", "No newer stable release")
            return None
        commit = self.github("commits/" + tag)
        revision = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise DeploymentError("Release did not resolve to an exact commit")
        release = {"tag": tag, "version": version, "revision": revision}
        self.state["release"] = release
        self.phase("available", f"Stable release {version} is available")
        return release

    def clean_source(self) -> None:
        changed = self.host.git("diff", "--name-only", "HEAD").splitlines()
        staged = self.host.git("diff", "--cached", "--name-only").splitlines()
        untracked = self.host.git("ls-files", "--others", "--exclude-standard").splitlines()
        blocked = sorted(set(staged + untracked + [p for p in changed if p not in LOCAL_CONFIG]))
        if blocked:
            raise DeploymentError("Local source/index changes block installation: " + ", ".join(blocked))
        if self.host.git("ls-files", "--unmerged"):
            raise DeploymentError("Unmerged Git index blocks installation")

    def stage(self, release: dict) -> tuple[Path, dict]:
        self.clean_source()
        stage = self.root / ("stage-" + release["revision"])
        token_env = {}
        token_path = Path(self.policy.get("token_file", "/etc/desktop-assistant/updater-github-token"))
        if token_path.is_file():
            token_env = {
                "VERA_GITHUB_TOKEN": token_path.read_text().strip(),
                "GIT_ASKPASS": "/opt/vera-updater/git-askpass",
            }
        if not stage.exists():
            stage.mkdir(mode=0o700)
            self.host.run(["git", "init", str(stage)])
        self.host.run(["git", "-C", str(stage), "fetch",
                       REPOSITORY_URL, "refs/tags/" + release["tag"]], timeout=300, env=token_env)
        fetched = self.host.run(["git", "-C", str(stage), "rev-parse", "FETCH_HEAD^{commit}"]).strip()
        if fetched != release["revision"]:
            raise DeploymentError("Release tag moved during staging")
        self.host.run(["git", "-C", str(stage), "checkout", "--detach", fetched])
        for path in stage.rglob("*"):
            if ".git" not in path.relative_to(stage).parts and path.is_symlink():
                raise DeploymentError("Release symlinks are not permitted in deployment staging")
        manifest = load_manifest(stage)
        if manifest["version"] != release["version"]:
            raise DeploymentError("Tag, manifest, and VERSION must agree")
        if (stage / ".gitmodules").exists():
            raise DeploymentError("This manifest schema does not support submodule deployment")
        if self.host.platform() not in manifest["platforms"]:
            raise DeploymentError("Release does not support this device platform")
        if shutil.disk_usage(self.root).free < manifest["minimum_free_bytes"]:
            raise DeploymentError("Insufficient free space to install and recover")
        self.host.run(["/usr/bin/python3", "-m", "compileall", "-q", str(stage / "src")])
        self.stage_assets(stage, manifest)
        return stage, manifest

    def stage_assets(self, stage: Path, manifest: dict) -> None:
        import yaml

        config = yaml.safe_load((self.checkout / "config/assistant.yaml").read_text())
        voice = config.get("tts", {}).get("piper_voice_name", "en_US-lessac-medium")
        override = config.get("tts", {}).get("piper_model")
        if override and not Path(override).is_file():
            raise DeploymentError("Configured custom Piper model is missing; repair it before updating")
        for asset in manifest["assets"]:
            if asset["when"] == "piper" and (override or Path(asset["path"]).stem != voice):
                continue
            if asset["when"] == "hailo" and not Path("/dev/hailo0").exists():
                continue
            target = stage / asset["path"]
            local = self.checkout / asset["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            if local.is_file() and sha256(local) == asset["sha256"]:
                shutil.copy2(local, target)
                continue
            self.phase("staging", f"Downloading verified asset {asset['path']}")
            temporary = target.with_suffix(target.suffix + ".download")
            try:
                with urllib.request.urlopen(asset["url"], timeout=60) as response, temporary.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                if sha256(temporary) != asset["sha256"]:
                    raise DeploymentError(f"Asset checksum mismatch: {asset['path']}")
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            if asset["when"] == "piper" and not target.with_suffix(".onnx.json").is_file():
                raise DeploymentError("Piper voice is missing its release-pinned configuration")
        if not override and not (stage / "config/piper" / (voice + ".onnx")).is_file():
            local = self.checkout / "config/piper" / (voice + ".onnx")
            if not local.is_file():
                raise DeploymentError("Configured Piper voice is missing and not declared in this release")

    def scheduled(self) -> None:
        window = self.state["settings"]
        if not window["enabled"]:
            return
        if time.time() - self.state.get("check_time", 0) < 3600:
            release = self.state.get("release")
        else:
            release = self.check()
        if release and in_window(window, dt.datetime.now().astimezone()):
            if release["revision"] != self.state.get("failed_revision"):
                self.install(release)

    def install(self, release: dict | None = None, retry: bool = False) -> None:
        release = release or self.check()
        if release is None:
            return
        if release["revision"] == self.state.get("failed_revision") and not retry:
            raise DeploymentError("This release previously failed; explicit retry is required")
        self.phase("preparing", "Checking local state and staging release")
        stage, manifest = self.stage(release)
        self.clean_source()
        units = self.host.units(manifest["units"] + manifest["retired_units"])
        if not {"desktop-assistant-core.service", "desktop-assistant-thermal.service",
                "desktop-assistant-web.service"}.issubset(u["name"] for u in units if u["active"]):
            raise DeploymentError("Core, thermal, and web must be active before deployment")
        old = self.host.git("rev-parse", "HEAD")
        branch = self.host.git("branch", "--show-current")
        # Private staging is intentionally unreadable to the device user.
        # Transfer only Git objects through a root-owned, read-only bundle;
        # never expose configuration backups or root credentials.
        bundle = self.host.control_dir / (release["revision"] + ".bundle")
        self.host.run(["git", "-C", str(stage), "bundle", "create", str(bundle), "HEAD"])
        bundle.chmod(0o644)
        try:
            self.host.git("fetch", "--no-tags", str(bundle), "HEAD")
        finally:
            bundle.unlink(missing_ok=True)
        if branch:
            self.host.git("merge-base", "--is-ancestor", old, release["revision"])
        backup = self.root / (f"backup-{old}-{time.time_ns()}")
        backup.mkdir(mode=0o700)
        shutil.copytree(self.checkout / "config", backup / "config", symlinks=True)
        defaults = {}
        for name in ("assistant.yaml", "thermal.yaml"):
            defaults[name] = self.host.git("show", old + ":config/" + name)
        for index, unit in enumerate(units):
            path = Path(unit["path"])
            if path.is_symlink():
                raise DeploymentError(f"Linked unit needs manual conversion before updating: {path}")
            if unit["scope"] == "user" and path.parent != self.host.home / ".config/systemd/user":
                raise DeploymentError("User unit must be installed in the standard per-user unit directory")
            shutil.copy2(path, backup / f"unit-{index}")
        for name in ("vera-updater.service", "vera-updater-check.service", "vera-updater.timer"):
            path = self.host.system_unit_dir / name
            if path.exists():
                shutil.copy2(path, backup / name)
        udev = self.host.udev_path
        if udev.exists():
            shutil.copy2(udev, backup / "udev")
        self.state["transaction"] = {
            "old": old, "old_version": (self.checkout / "VERSION").read_text().strip(),
            "branch": branch, "stage": str(stage), "backup": str(backup),
            "manifest": manifest, "release": release, "units": units,
            "defaults": defaults, "boot": boot_id(), "mutated": False,
            "new_units": [], "started_after": time.time(), "source_changed": False,
        }
        self.save()
        try:
            self.deploy()
        except Exception:
            log.exception("Deployment failed")
            self.recover()
            raise

    def deploy(self) -> None:
        transaction = self.state["transaction"]
        manifest = transaction["manifest"]
        stage, backup = Path(transaction["stage"]), Path(transaction["backup"])
        units = transaction["units"]
        self.phase("installing", "Installing declared dependencies and release")
        # Establish full-speed protection before any global dependency mutation.
        fan_path = self.host.fan_safe()
        transaction["fan_path"] = str(fan_path)
        transaction["mutated"] = True
        self.save()
        watchdog = next((u for u in units if u["name"].endswith("watchdog.service")), None)
        if watchdog and watchdog["active"]:
            self.host.systemctl(watchdog["scope"], "stop", watchdog["name"])
        self.host.run(["apt-get", "-o", "DPkg::Lock::Timeout=120", "update"], timeout=600)
        self.host.run(["apt-get", "-o", "DPkg::Lock::Timeout=120", "install", "-y",
                       *manifest["apt"]], timeout=1800, env={"NEEDRESTART_MODE": "l"})
        if manifest["python_overlay"]:
            self.host.run(["/usr/bin/python3", "-m", "pip", "install",
                           "--root-user-action=ignore", "--break-system-packages",
                           "--ignore-installed", *manifest["python_overlay"]], timeout=900)
        self.host.run(["/usr/bin/python3", "-m", "pip", "install",
                       "--root-user-action=ignore", "--break-system-packages",
                       "-r", str(stage / "requirements.txt")], timeout=1800)
        self.host.run(["/usr/bin/python3", "-c",
                       ";".join("import " + name for name in manifest["imports"])])
        self.host.run(["bash", str(stage / "scripts/build_scrfd_decode.sh")], timeout=900)
        self.clean_source()
        for unit in reversed(units):
            if unit["active"] and not unit["name"].endswith(("watchdog.service", "thermal.service")):
                self.host.systemctl(unit["scope"], "stop", unit["name"])
        # Freeze runtime-written configuration only once application writers stop.
        shutil.copytree(self.checkout / "config", backup / "config", symlinks=True, dirs_exist_ok=True)
        self.clean_source()
        for name in LOCAL_CONFIG:
            self.host.git("restore", "--source=HEAD", "--", name)
        release = transaction["release"]
        # Persist before touching HEAD so a crash during checkout is recoverable.
        transaction["source_changed"] = True
        self.save()
        if transaction["branch"]:
            self.host.git("merge", "--ff-only", release["revision"])
        else:
            self.host.git("checkout", "--detach", release["revision"])
        self.apply_config(stage, backup, transaction)
        for asset in manifest["assets"]:
            source = stage / asset["path"]
            if source.is_file():
                self.copy_owned(source, self.checkout / asset["path"])
        for source in (stage / "src/perception/scrfd_decode").glob("*.so"):
            self.copy_owned(source, self.checkout / "src/perception/scrfd_decode" / source.name)
        self.update_units(stage, backup, transaction)
        self.configure_boot(backup, transaction)
        self.host.run(["/usr/bin/python3", "-m", "compileall", "-q", str(self.checkout / "src")], user=True)
        thermal = next(u for u in units if u["name"].endswith("thermal.service"))
        self.host.systemctl(thermal["scope"], "stop", thermal["name"])
        # Older thermal versions disabled PWM on shutdown; repair and verify
        # the kernel-owned full-duty output before starting the new process.
        period = (fan_path / "period").read_text()
        (fan_path / "duty_cycle").write_text(period)
        (fan_path / "enable").write_text("1")
        self.host.verify_fan(fan_path)
        transaction["started_after"] = time.time()
        self.save()
        for unit in units + transaction["new_units"]:
            if unit["active"] and unit["name"] not in manifest["retired_units"] and not unit["name"].endswith("watchdog.service"):
                self.host.systemctl(unit["scope"], "start", unit["name"])
        reboot = manifest["reboot"] == "required" or (
            manifest["reboot"] == "if-required" and (
                Path("/var/run/reboot-required").exists() or transaction.get("boot_changed")
            )
        )
        if reboot:
            self.state["pending_reboot"] = True
            self.phase("pending_reboot", "Release installed; reboot required before validation")
            self.host.run(["systemctl", "reboot"])
            return
        self.finish()

    def configure_boot(self, backup: Path, transaction: dict) -> None:
        lines = transaction["manifest"]["boot_lines"]
        path = self.host.boot_config
        if not path.is_file():
            raise DeploymentError("Cannot locate Raspberry Pi boot configuration")
        current = path.read_text()
        missing = [line for line in lines if line not in current.splitlines()]
        if not missing:
            return
        if transaction["manifest"]["reboot"] == "never":
            raise DeploymentError("Release requires boot changes but forbids the necessary reboot")
        shutil.copy2(path, backup / "boot-config")
        transaction.update(boot_config=str(path), boot_changed=True)
        self.save()
        # Explicit all section avoids accidentally inserting hardware settings
        # into a model-specific/conditional section at the end of config.txt.
        path.write_text(current.rstrip() + "\n\n[all]\n" + "\n".join(missing) + "\n")

    def copy_owned(self, source: Path, destination: Path) -> None:
        self.host.write_user_file(destination, source.read_bytes(), source.stat().st_mode & 0o777)

    def apply_config(self, stage: Path, backup: Path, transaction: dict) -> None:
        import yaml

        for name in ("assistant.yaml", "thermal.yaml"):
            old = yaml.safe_load(transaction["defaults"][name]) or {}
            new = yaml.safe_load((stage / "config" / name).read_text()) or {}
            local = yaml.safe_load((backup / "config" / name).read_text()) or {}
            for migration in transaction["manifest"]["migrations"]:
                if migration["file"] == name:
                    for document in (old, local):
                        source_keys = migration["from"].split(".")
                        target_keys = migration["to"].split(".")
                        source = document
                        for key in source_keys[:-1]:
                            source = source.get(key, {})
                        if source_keys[-1] not in source:
                            continue
                        target = document
                        for key in target_keys[:-1]:
                            target = target.setdefault(key, {})
                        if target_keys[-1] in target:
                            raise DeploymentError("Configuration rename collides with an existing key")
                        target[target_keys[-1]] = source.pop(source_keys[-1])
            merged = merge_defaults(old, new, local)
            target = self.checkout / "config" / name
            self.host.write_user_file(target, yaml.safe_dump(merged, sort_keys=False).encode())
        self.copy_owned(backup / "config/stereo_cal.npz", self.checkout / "config/stereo_cal.npz")

    def update_units(self, stage: Path, backup: Path, transaction: dict) -> None:
        units = transaction["units"]
        manifest = transaction["manifest"]
        scopes = set()
        for name in manifest["units"]:
            current = next((u for u in units if u["name"] == name), None)
            scope = current["scope"] if current else self.policy["default_scope"]
            destination = (Path(current["path"]) if current else
                           self.home / ".config/systemd/user" / name if scope == "user" else
                           self.host.system_unit_dir / name)
            if not current:
                current = {"name": name, "scope": scope, "path": str(destination), "active": True}
                transaction["new_units"].append(current)
                self.save()
            text = render_unit((stage / "services/systemd" / name).read_text(), self.policy, scope)
            if destination.exists():
                old_template = self.host.git("show", transaction["old"] + ":services/systemd/" + name)
                previous = render_unit(old_template, self.policy, scope)
                text = preserve_overrides(previous, destination.read_text(), text)
            if name.endswith("watchdog.service"):
                layout = {
                    unit["name"].removeprefix("desktop-assistant-").removesuffix(".service"): unit["scope"]
                    for unit in units + transaction["new_units"]
                }
                for managed in manifest["units"]:
                    short = managed.removeprefix("desktop-assistant-").removesuffix(".service")
                    if short != "watchdog":
                        key = "VERA_SYSTEMD_SCOPE_" + short.upper()
                        text = re.sub(rf"(?m)^Environment=\"?{key}=[^\n]*\n?", "", text)
                        text = text.replace("[Service]\n", "[Service]\nEnvironment=" + key + "=" +
                                            layout.get(short, self.policy["default_scope"]) + "\n", 1)
            if scope == "user":
                self.host.write_user_file(destination, text.encode())
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(text)
                destination.chmod(0o644)
            scopes.add(scope)
        for name in manifest["retired_units"]:
            unit = next((u for u in units if u["name"] == name), None)
            if unit:
                self.host.systemctl(unit["scope"], "disable", "--now", name)
                Path(unit["path"]).unlink()
                scopes.add(unit["scope"])
        udev = self.host.udev_path
        shutil.copy2(stage / "services/udev/70-respeaker-flex-xvf.rules", udev)
        self.host.run(["udevadm", "control", "--reload-rules"])
        for scope in scopes:
            self.host.systemctl(scope, "daemon-reload")
        for unit in transaction["new_units"]:
            self.host.systemctl(unit["scope"], "enable", unit["name"])

    def finish(self) -> None:
        transaction = self.state["transaction"]
        self.phase("verifying", "Checking loaded revision and fresh hardware/service health")
        active_units = [unit for unit in transaction["units"] + transaction["new_units"]
                        if unit["name"] not in transaction["manifest"]["retired_units"]]
        self.host.readiness(active_units,
                            transaction["release"]["revision"], transaction["release"]["version"],
                            transaction["manifest"], transaction["started_after"])
        self.restore_watchdog(transaction)
        # Upgrade the root-owned runner only after device validation.
        if self.policy.get("runner"):
            runner = Path(self.policy["runner"])
            candidate = runner.with_name(runner.name + ".next")
            candidate.mkdir(mode=0o755, exist_ok=True)
            shutil.copytree(Path(transaction["stage"]) / "src/updater", candidate / "src/updater",
                            ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True)
            (candidate / "src/__init__.py").touch()
            shutil.copy2(Path(transaction["stage"]) / "scripts/updater-git-askpass", candidate / "git-askpass")
            (candidate / "git-askpass").chmod(0o700)
            self.host.run(["/usr/bin/python3", "-m", "compileall", "-q", str(candidate)])
            old_runner = runner.with_name(runner.name + ".previous")
            if old_runner.exists():
                shutil.rmtree(old_runner)
            transaction["runner_changed"] = True
            self.save()
            runner.rename(old_runner)
            candidate.rename(runner)
            for name in ("vera-updater.service", "vera-updater-check.service", "vera-updater.timer"):
                shutil.copy2(Path(transaction["stage"]) / "services/systemd" / name,
                             self.host.system_unit_dir / name)
            self.host.systemctl("system", "daemon-reload")
        self.state.update(pending_reboot=False, failed_revision=None, error=None)
        self.phase("success", f"Release {transaction['release']['version']} passed device readiness")
        self.state.pop("transaction", None)
        self.state["release"] = None
        previous_backup = self.state.get("previous_backup")
        self.state["previous_backup"] = transaction["backup"]
        self.save()
        # Delete only this transaction's explicitly resolved staging directory.
        try:
            shutil.rmtree(Path(transaction["stage"]))
            if previous_backup and previous_backup != transaction["backup"]:
                previous = Path(previous_backup)
                if previous.parent != self.root or not previous.name.startswith("backup-"):
                    raise DeploymentError("Unsafe old backup path in updater state")
                shutil.rmtree(previous)
            if self.policy.get("runner"):
                # Restart only after durable success; the new root-owned runner
                # resumes normal operation without an active transaction.
                self.host.systemctl("system", "--no-block", "restart", "vera-updater.service")
        except (OSError, DeploymentError) as exc:
            log.exception("Device is updated, but post-success maintenance failed")
            self.state["error"] = f"Device passed readiness; updater maintenance needs attention: {exc}"
            self.save()

    def restore_watchdog(self, transaction: dict) -> None:
        units = transaction["units"] if self.state["phase"] == "recovering" else transaction["units"] + transaction["new_units"]
        for unit in units:
            if (unit["name"].endswith("watchdog.service") and unit["active"] and
                    (self.state["phase"] == "recovering" or unit["name"] not in transaction["manifest"]["retired_units"])):
                self.host.systemctl(unit["scope"], "start", unit["name"])

    def resume(self) -> None:
        transaction = self.state.get("transaction")
        if not transaction:
            return
        if self.state["phase"] == "pending_reboot" and transaction["boot"] != boot_id():
            try:
                self.finish()
            except Exception:
                log.exception("Post-reboot validation failed")
                self.recover()
        elif self.state["phase"] == "pending_reboot":
            raise DeploymentError("Reboot is still required; do not repeat installation")
        else:
            self.recover()

    def recover(self) -> None:
        transaction = self.state["transaction"]
        self.state["failed_revision"] = transaction["release"]["revision"]
        self.phase("recovering", "Restoring previous code/config/services; global packages may need manual repair")
        try:
            if transaction["mutated"]:
                units = transaction["units"]
                for unit in reversed(units + transaction["new_units"]):
                    if not unit["name"].endswith("thermal.service"):
                        self.host.systemctl(unit["scope"], "stop", unit["name"])
                if transaction["source_changed"]:
                    for name in LOCAL_CONFIG:
                        self.host.git("restore", "--source=HEAD", "--", name)
                    self.host.git("checkout", "--detach", transaction["old"])
                    if transaction["branch"]:
                        self.host.git("branch", "-f", transaction["branch"], transaction["old"])
                        self.host.git("checkout", transaction["branch"])
                backup = Path(transaction["backup"])
                for path in (backup / "config").rglob("*"):
                    if path.is_file() and not path.is_symlink():
                        self.copy_owned(path, self.checkout / "config" / path.relative_to(backup / "config"))
                for index, unit in enumerate(units):
                    if unit["scope"] == "user":
                        self.copy_owned(backup / f"unit-{index}", Path(unit["path"]))
                    else:
                        shutil.copy2(backup / f"unit-{index}", unit["path"])
                for unit in transaction["new_units"]:
                    self.host.systemctl(unit["scope"], "disable", unit["name"])
                    Path(unit["path"]).unlink(missing_ok=True)
                if (backup / "udev").exists():
                    shutil.copy2(backup / "udev", self.host.udev_path)
                    self.host.run(["udevadm", "control", "--reload-rules"])
                if transaction.get("boot_changed"):
                    shutil.copy2(backup / "boot-config", transaction["boot_config"])
                for scope in set(u["scope"] for u in units + transaction["new_units"]):
                    self.host.systemctl(scope, "daemon-reload")
                for unit in units:
                    if unit["name"] in transaction["manifest"]["retired_units"] and unit.get("enabled"):
                        self.host.systemctl(unit["scope"], "enable", unit["name"])
                if transaction.get("runner_changed"):
                    runner = Path(self.policy["runner"])
                    old_runner = runner.with_name(runner.name + ".previous")
                    if not old_runner.exists():
                        raise DeploymentError("Previous updater runner is missing")
                    if runner.exists():
                        failed_runner = runner.with_name(runner.name + f".failed-{time.time_ns()}")
                        runner.rename(failed_runner)
                    old_runner.rename(runner)
                    for name in ("vera-updater.service", "vera-updater-check.service", "vera-updater.timer"):
                        if (backup / name).exists():
                            shutil.copy2(backup / name, self.host.system_unit_dir / name)
                    self.host.systemctl("system", "daemon-reload")
                self.host.run(["bash", str(self.checkout / "scripts/build_scrfd_decode.sh")], user=True, timeout=900)
                thermal = next(u for u in units if u["name"].endswith("thermal.service"))
                fan = Path(transaction["fan_path"])
                self.host.systemctl(thermal["scope"], "stop", thermal["name"])
                period = (fan / "period").read_text()
                (fan / "duty_cycle").write_text(period)
                (fan / "enable").write_text("1")
                self.host.verify_fan(fan)
                restarted = time.time()
                for unit in units:
                    if unit["active"] and not unit["name"].endswith("watchdog.service"):
                        self.host.systemctl(unit["scope"], "start", unit["name"])
                self.host.readiness(units, transaction["old"], transaction["old_version"],
                                    transaction["manifest"], restarted)
            self.restore_watchdog(transaction)
            self.state.update(pending_reboot=False, error="Update failed; previous application restored. Global package changes remain.")
            self.phase("rolled_back", self.state["error"])
            self.state.pop("transaction", None)
            self.save()
        except Exception as exc:
            log.exception("Recovery failed")
            self.state["error"] = f"Recovery failed: {exc}. Manual repair required; global package/driver changes cannot be rolled back."
            self.phase("manual_repair", self.state["error"])
            raise DeploymentError(self.state["error"]) from exc


def render_unit(text: str, policy: dict, scope: str) -> str:
    text = text.replace("/home/starter/Code/Desktop Assistant", policy["checkout"])
    text = text.replace("/home/starter", policy["home"])
    text = text.replace("User=starter", "User=" + policy["user"]).replace("Group=starter", "Group=" + policy["group"])
    text = text.replace("/run/user/1000", "/run/user/" + str(policy["uid"]))
    if scope == "user":
        text = "\n".join(line for line in text.splitlines() if not line.startswith(
            ("User=", "Group=", "SupplementaryGroups=", "ProtectSystem=", "ProtectHome=", "ReadWritePaths=")
        ))
        # Remove continuation lines belonging to ReadWritePaths.
        text = re.sub(r"(?m)^    /sys/[^\n]*\n?", "", text)
        text = text.replace("WantedBy=multi-user.target", "WantedBy=default.target")
    return text.rstrip() + "\n"


def directives(text: str) -> dict[tuple[str, str], list[str]]:
    result: dict[tuple[str, str], list[str]] = {}
    section = ""
    lines = iter(text.splitlines())
    for line in lines:
        if line.startswith("["):
            section = line
        elif "=" in line and not line.lstrip().startswith(("#", ";")):
            key = line.split("=", 1)[0]
            while line.endswith("\\"):
                line = line[:-1] + next(lines)
            result.setdefault((section, key), []).append(line)
    return result


def preserve_overrides(previous: str, local: str, incoming: str) -> str:
    old, actual = directives(previous), directives(local)
    overrides = {key: value for key, value in actual.items() if value != old.get(key)}
    # A deployed executable must come from the new release, not a stale command.
    if ("[Service]", "ExecStart") in overrides:
        raise DeploymentError("Custom ExecStart requires manual service migration")
    section = ""
    output = []
    consumed = set()
    lines = iter(incoming.splitlines())
    for line in lines:
        if line.startswith("["):
            for key, values in overrides.items():
                if key[0] == section and key not in consumed:
                    output.extend(values)
                    consumed.add(key)
            section = line
        key = (section, line.split("=", 1)[0])
        if "=" in line and key in overrides and not line.lstrip().startswith(("#", ";")):
            while line.endswith("\\"):
                line = next(lines)
            if key not in consumed:
                output.extend(overrides[key])
                consumed.add(key)
        else:
            output.append(line)
    for key, values in overrides.items():
        if key[0] == section and key not in consumed:
            output.extend(values)
    return "\n".join(output) + "\n"
