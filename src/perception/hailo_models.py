"""Hardware-specific model selection and installation.

Run ``python3 -m src.perception.hailo_models --install`` after installing HailoRT.
Profiles, candidate paths and download URLs live in config/hailo/models.yaml.
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path
import subprocess
import tempfile
import urllib.request

import yaml

from src.perception.hailo_probe import probe_hailo

log = logging.getLogger(__name__)
_ROOT = Path(__file__).resolve().parents[2]
_CONFIG = _ROOT / "config" / "hailo" / "models.yaml"


def _profile() -> tuple[str, dict]:
    with _CONFIG.open() as stream:
        config = yaml.safe_load(stream)
    family = config["device"]
    if family == "auto":
        family = probe_hailo().family
    if family not in config["profiles"]:
        if family != "unknown":
            raise ValueError(f"Unknown Hailo model profile: {family}")
        return family, {}
    return family, config["profiles"][family]


def _installed_path(family: str, spec: dict) -> Path:
    return _ROOT / "config" / "hailo" / family.lower().replace("-", "") / spec["file"]


def resolve_model(kind: str, override: str | Path | None = None) -> Path | None:
    """Select only models for this device; explicit paths are never substituted."""
    if override and str(override) != "auto":
        path = Path(override).expanduser()
        if not path.is_absolute():
            path = _ROOT / path
        if path.is_file():
            return path
        log.warning("Explicit %s model not found: %s", kind, path)
        return None
    family, profile = _profile()
    if not profile:
        log.warning("No %s model profile for accelerator %s", kind, family)
        return None
    spec = profile[kind]
    candidates = [_installed_path(family, spec)]
    candidates.extend(Path(path) for path in spec["candidates"])
    for path in candidates:
        path = path if path.is_absolute() else _ROOT / path
        if path.is_file():
            return path
    log.warning(
        "No %s model installed for %s; run python3 -m src.perception.hailo_models --install",
        kind, family,
    )
    return None


def install_models() -> None:
    family, profile = _profile()
    if not profile:
        raise RuntimeError("Cannot install models: no supported accelerator detected")
    for kind, spec in profile.items():
        if resolve_model(kind) is not None:
            continue
        destination = _installed_path(family, spec)
        destination.parent.mkdir(parents=True, exist_ok=True)
        log.info("Downloading %s for %s", kind, family)
        with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
            temporary = Path(directory) / spec["file"]
            with urllib.request.urlopen(spec["url"], timeout=120) as response:
                with temporary.open("wb") as stream:
                    while chunk := response.read(1024 * 1024):
                        stream.write(chunk)
            try:
                subprocess.run(
                    ["hailortcli", "parse-hef", str(temporary)],
                    check=True, capture_output=True, text=True,
                )
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(
                    f"Invalid {kind} model for {family}: {exc.stderr or exc.stdout}"
                ) from exc
            temporary.replace(destination)
        log.info("Installed %s", destination)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--install", action="store_true", required=True)
    parser.parse_args()
    install_models()
