"""Validated release metadata and configuration migration."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

REPOSITORY = "mazum01/desktop-assistant"
REPOSITORY_URL = f"https://github.com/{REPOSITORY}.git"
VERSION_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
UNIT_RE = re.compile(r"desktop-assistant-[a-z-]+\.service")


def version_tuple(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise ValueError("Expected a stable MAJOR.MINOR.PATCH version")
    return tuple(map(int, value.split(".")))


def settings(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != {"enabled", "start", "end"}:
        raise ValueError("Settings require enabled, start, and end")
    if type(value["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    for key in ("start", "end"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value[key]
        ):
            raise ValueError("Window times must be HH:MM")
    if value["start"] == value["end"]:
        raise ValueError("Start and end must differ")
    return dict(value)


def in_window(value: dict, now: dt.datetime) -> bool:
    clock = now.strftime("%H:%M")
    start, end = value["start"], value["end"]
    return start <= clock < end if start < end else clock >= start or clock < end


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("Missing relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or "\\" in value or value.startswith(".git"):
        raise ValueError(f"Unsafe release path: {value}")
    return value


def load_manifest(root: Path) -> dict:
    path = root / "config" / "deployment.json"
    if not path.is_file():
        raise ValueError("Release has no supported deployment manifest; install manually")
    value = json.loads(path.read_text())
    required = {
        "schema", "version", "platforms", "apt", "python_overlay", "imports",
        "units", "retired_units", "assets", "migrations", "reboot",
        "boot_lines",
        "minimum_free_bytes", "health_timeout_seconds", "observation_seconds",
    }
    if not isinstance(value, dict) or set(value) != required or value["schema"] != 1:
        raise ValueError("Unsupported deployment manifest schema or fields")
    version_tuple(value["version"])
    if value["version"] != (root / "VERSION").read_text().strip():
        raise ValueError("Manifest and VERSION disagree")
    if value["reboot"] not in ("never", "required", "if-required"):
        raise ValueError("Invalid reboot policy")
    if not isinstance(value["boot_lines"], list) or any(
        line not in ("dtparam=i2c_arm=on", "dtoverlay=pwm,pin=13,func=4")
        for line in value["boot_lines"]
    ):
        raise ValueError("Unsupported boot hardware configuration")
    if not isinstance(value["platforms"], list) or not value["platforms"]:
        raise ValueError("Missing platforms")
    if any(p not in ("aarch64:bookworm", "aarch64:trixie") for p in value["platforms"]):
        raise ValueError("Unsupported platform")
    for key in ("apt", "python_overlay", "imports", "units", "retired_units", "assets", "migrations"):
        if not isinstance(value[key], list):
            raise ValueError(f"{key} must be a list")
    for package in value["apt"]:
        if not isinstance(package, str) or not re.fullmatch(r"[a-z0-9][a-z0-9+.-]*(?:=[a-zA-Z0-9:~+._-]+)?", package):
            raise ValueError("Invalid APT package")
    for package in value["python_overlay"] + value["imports"]:
        if not isinstance(package, str) or not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.-]*", package):
            raise ValueError("Invalid Python package/import")
    for unit in value["units"] + value["retired_units"]:
        if not isinstance(unit, str) or not UNIT_RE.fullmatch(unit):
            raise ValueError("Invalid managed unit")
    essential = {
        "desktop-assistant-core.service", "desktop-assistant-thermal.service",
        "desktop-assistant-web.service",
    }
    if not essential.issubset(value["units"]):
        raise ValueError("Release must retain core, thermal, and web services")
    if len(set(value["units"])) != len(value["units"]) or set(value["units"]) & set(value["retired_units"]):
        raise ValueError("Duplicate or simultaneously managed/retired unit")
    for unit in value["units"]:
        if not (root / "services/systemd" / unit).is_file():
            raise ValueError(f"Missing release service template: {unit}")
    for path in ("requirements.txt", "scripts/build_scrfd_decode.sh",
                 "services/udev/70-respeaker-flex-xvf.rules"):
        if not (root / path).is_file():
            raise ValueError(f"Missing required deployment input: {path}")
    for key in ("minimum_free_bytes", "health_timeout_seconds", "observation_seconds"):
        if type(value[key]) is not int or value[key] <= 0:
            raise ValueError(f"{key} must be positive")
    if value["health_timeout_seconds"] < value["observation_seconds"]:
        raise ValueError("Health timeout is shorter than observation interval")
    for asset in value["assets"]:
        if not isinstance(asset, dict) or set(asset) != {"path", "url", "sha256", "when"}:
            raise ValueError("Invalid asset declaration")
        relative_path(asset["path"])
        if not asset["path"].startswith("config/"):
            raise ValueError("Assets must be inside config/")
        parsed = urlparse(asset["url"])
        if parsed.scheme != "https" or parsed.hostname not in ("github.com", "huggingface.co"):
            raise ValueError("Asset must come from an approved HTTPS host")
        if parsed.username or parsed.password or not re.fullmatch(r"[0-9a-f]{64}", asset["sha256"]):
            raise ValueError("Asset requires a SHA256 checksum and credential-free URL")
        if asset["when"] not in ("always", "hailo", "piper"):
            raise ValueError("Invalid asset capability")
    for migration in value["migrations"]:
        if not isinstance(migration, dict) or set(migration) != {"file", "from", "to"}:
            raise ValueError("Only declarative configuration renames are supported")
        if migration["file"] not in ("assistant.yaml", "thermal.yaml"):
            raise ValueError("Unsupported migration file")
        for key in ("from", "to"):
            if not isinstance(migration[key], str) or not re.fullmatch(r"[a-zA-Z0-9_.]+", migration[key]):
                raise ValueError("Invalid configuration key")
    return value


def merge_defaults(old: dict, new: dict, local: dict) -> dict:
    """Three-way merge defaults without replacing local choices."""
    result = copy.deepcopy(local)
    for key, previous in old.items():
        if key not in new and key in local:
            raise ValueError(f"Removed configuration key {key} needs an explicit migration")
        if key in new and key in local:
            incoming, current = new[key], local[key]
            if type(previous) is not type(incoming):
                raise ValueError(f"Configuration type changed for {key}; manual migration required")
            if isinstance(previous, dict):
                if not isinstance(current, dict):
                    raise ValueError(f"Local configuration type mismatch for {key}")
                result[key] = merge_defaults(previous, incoming, current)
            elif current == previous:
                result[key] = copy.deepcopy(incoming)
    for key, incoming in new.items():
        if key not in result:
            result[key] = copy.deepcopy(incoming)
    return result
