"""Bounded system commands and device-aware service/readiness operations."""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import time
from pathlib import Path

log = logging.getLogger(__name__)


class DeploymentError(RuntimeError):
    """An update could not be safely completed."""


class Host:
    def __init__(self, policy: dict):
        self.policy = policy
        self.checkout = Path(policy["checkout"])
        self.user = policy["user"]
        self.uid = policy["uid"]
        self.home = Path(policy["home"])
        self.system_unit_dir = Path("/etc/systemd/system")
        self.control_dir = Path("/run/vera-updater")
        self.udev_path = Path("/etc/udev/rules.d/70-respeaker-flex-xvf.rules")
        self.boot_config = Path("/boot/firmware/config.txt")
        if not self.boot_config.exists():
            self.boot_config = Path("/boot/config.txt")

    def run(self, args: list[str], *, cwd: Path | None = None,
            user: bool = False, timeout: int = 120, env: dict | None = None,
            input_data: bytes | None = None) -> str:
        environment = {
            **os.environ, "GIT_TERMINAL_PROMPT": "0", "DEBIAN_FRONTEND": "noninteractive",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        }
        if env:
            environment.update(env)
        if user:
            args = ["runuser", "-u", self.user, "--", "env",
                    f"HOME={self.home}", f"XDG_RUNTIME_DIR=/run/user/{self.uid}",
                    f"DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{self.uid}/bus", *args]
        try:
            result = subprocess.run(args, cwd=cwd, env=environment, capture_output=True,
                                    text=input_data is None, input=input_data, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DeploymentError(f"{args[-1]}: command unavailable or timed out") from exc
        if result.returncode:
            output = result.stderr or result.stdout
            if isinstance(output, bytes):
                output = output.decode(errors="replace")
            detail = output[-2000:].strip()
            token = environment.get("VERA_GITHUB_TOKEN")
            if token:
                detail = detail.replace(token, "[redacted]")
            raise DeploymentError(f"Command failed ({result.returncode}): {detail}")
        return result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else result.stdout

    def write_user_file(self, destination: Path, data: bytes, mode: int = 0o644) -> None:
        # All writes into user-controlled directories run as the device account.
        # Even a concurrently replaced parent symlink cannot grant root writes.
        self.run([
            "/usr/bin/python3", "-c",
            "import os,pathlib,sys;"
            "p=pathlib.Path(sys.argv[1]);p.parent.mkdir(parents=True,exist_ok=True);"
            "fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,int(sys.argv[2]));"
            "f=os.fdopen(fd,'wb');f.write(sys.stdin.buffer.read());f.close()",
            str(destination), str(mode),
        ], user=True, input_data=data)

    def git(self, *args: str) -> str:
        return self.run(["git", "-c", "core.hooksPath=/dev/null", "-C",
                         str(self.checkout), *args], user=True).strip()

    def systemctl(self, scope: str, *args: str) -> str:
        return self.run(["systemctl", *(["--user"] if scope == "user" else []),
                         *args], user=scope == "user")

    def units(self, names: list[str]) -> list[dict]:
        result = []
        for name in names:
            found = []
            for scope in ("system", "user"):
                if scope == "user" and not Path(f"/run/user/{self.uid}/bus").exists():
                    log.info("No user systemd manager for UID %s; checking system units only", self.uid)
                    continue
                output = self.systemctl(scope, "show", name, "--property=LoadState",
                                        "--property=ActiveState", "--property=FragmentPath",
                                        "--property=UnitFileState")
                fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
                if fields.get("LoadState") == "loaded":
                    found.append({
                        "name": name, "scope": scope, "path": fields["FragmentPath"],
                        "active": fields["ActiveState"] == "active",
                        "enabled": fields.get("UnitFileState") == "enabled",
                    })
            if len(found) > 1:
                raise DeploymentError(f"Duplicate system/user unit: {name}")
            if found:
                result.extend(found)
        return result

    def platform(self) -> str:
        fields = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                      if "=" in line)
        return f"{platform.machine()}:{fields.get('VERSION_CODENAME', '').strip(chr(34))}"

    def rpc(self, name: str, command: dict) -> dict:
        import zmq

        endpoint = "ipc:///tmp/desktop-assistant"
        if name != "core":
            endpoint += f"-{name}"
        with zmq.Context() as context, context.socket(zmq.REQ) as connection:
            connection.setsockopt(zmq.LINGER, 0)
            connection.setsockopt(zmq.RCVTIMEO, 3000)
            connection.setsockopt(zmq.SNDTIMEO, 3000)
            connection.connect(endpoint + ".rep")
            connection.send_json(command)
            try:
                response = connection.recv_json()
            except zmq.ZMQError as exc:
                raise DeploymentError(f"{name} IPC readiness timeout") from exc
        if not response.get("ok"):
            raise DeploymentError(f"{name} IPC rejected {command.get('cmd')}")
        return response

    def fan_safe(self) -> Path:
        import yaml

        fan = yaml.safe_load((self.checkout / "config/thermal.yaml").read_text())["fan"]
        chip, channel = fan["pwm_chip"], fan["pwm_channel"]
        if type(chip) is not int or type(channel) is not int or chip < 0 or channel < 0:
            raise DeploymentError("Invalid PWM chip/channel")
        path = Path(f"/sys/class/pwm/pwmchip{chip}/pwm{channel}")
        ready = self.rpc("thermal", {"cmd": "deployment.readiness"})
        if ready.get("services", {}).get("thermal", {}).get("fan_backend") != "sysfs":
            raise DeploymentError("Safe unattended thermal restart requires kernel sysfs PWM")
        self.rpc("thermal", {"cmd": "publish", "topic": "thermal.fan.set_override",
                             "payload": {"duty": 100}})
        period = int((path / "period").read_text())
        (path / "duty_cycle").write_text(str(period))
        (path / "enable").write_text("1")
        self.verify_fan(path)
        return path

    @staticmethod
    def verify_fan(path: Path) -> None:
        period = int((path / "period").read_text())
        if period <= 0 or int((path / "duty_cycle").read_text()) != period or (path / "enable").read_text().strip() != "1":
            raise DeploymentError("Could not establish verified full-speed fan protection")

    def readiness(self, units: list[dict], revision: str, version: str,
                  manifest: dict, started_after: float) -> None:
        import urllib.request

        deadline = time.monotonic() + manifest["health_timeout_seconds"]
        stable_since = None
        previous_pids = None
        error = "No readiness samples"
        while time.monotonic() < deadline:
            try:
                pids = []
                for unit in units:
                    if unit["name"].endswith("watchdog.service") or not unit["active"]:
                        continue
                    if self.systemctl(unit["scope"], "is-active", unit["name"]).strip() != "active":
                        raise DeploymentError(f"{unit['name']} is not active")
                    name = unit["name"].removeprefix("desktop-assistant-").removesuffix(".service")
                    response = self.rpc(name, {"cmd": "deployment.readiness"})
                    if response["revision"] != revision or response["version"] != version:
                        raise DeploymentError(f"{name} loaded a different revision/version")
                    if response["started_at"] < started_after:
                        raise DeploymentError(f"{name} was not restarted")
                    for svc, details in response["services"].items():
                        if not details["running"] or details.get("hardware_ready") is False:
                            raise DeploymentError(f"{name}/{svc} is not ready")
                        if svc == "thermal" and (
                            not details.get("sensor_ok") or details.get("fan_backend") == "sim"
                        ):
                            raise DeploymentError("Thermal sensor/fan is not ready")
                    for topic in (("thermal.temp", "thermal.fan") if name == "thermal" else
                                  ("vision.frame_ready", "audio.level") if name == "core" else ()):
                        received = response["received_at"].get(topic, 0)
                        if time.time() - received > 10 or received < started_after:
                            raise DeploymentError(f"No fresh {topic} telemetry")
                    pids.append(response["pid"])
                with urllib.request.urlopen(self.policy["web_url"] + "/health", timeout=5) as reply:
                    if json.load(reply).get("ok") is not True:
                        raise DeploymentError("Web liveness failed")
                if previous_pids != pids:
                    stable_since = time.monotonic()
                previous_pids = pids
                if stable_since is not None and time.monotonic() - stable_since >= manifest["observation_seconds"]:
                    return
            except (DeploymentError, OSError, ValueError, KeyError) as exc:
                error = str(exc)
                stable_since = None
                previous_pids = None
            time.sleep(2)
        raise DeploymentError(f"Device readiness failed: {error}")
