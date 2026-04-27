"""
Hailo-8 AI accelerator probe and capability check.

This module does NOT run inference — it verifies the device is present,
the runtime is available, and the firmware is responsive. Inference will
be added in Phase 3 (face detection / depth estimation).

The check has three layers, each independently useful:

  1. PCIe presence — `lspci` shows the Hailo device (driver-independent).
  2. CLI presence  — `hailortcli` is installed and on PATH.
  3. Identify     — `hailortcli fw-control identify` returns device info.

Per project imperative: if the accelerator is unavailable, the assistant
must gracefully degrade to CPU inference. This probe returns a structured
status so callers can make that decision.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

_HAILO_PCI_VENDOR_ID = "1e60"
_HAILO_PCI_KEYWORD = "Hailo"


@dataclass
class HailoStatus:
    """Structured result of probing the Hailo-8 stack."""

    pcie_present: bool = False
    pcie_devices: List[str] = field(default_factory=list)

    cli_installed: bool = False
    cli_path: Optional[str] = None

    identify_ok: bool = False
    device_id: Optional[str] = None
    serial_number: Optional[str] = None
    firmware_version: Optional[str] = None
    board_name: Optional[str] = None

    error: Optional[str] = None

    @property
    def fully_ready(self) -> bool:
        """True only if the chip is present, the runtime is installed,
        and the firmware responded to an identify call."""
        return self.pcie_present and self.cli_installed and self.identify_ok

    def degrade_reason(self) -> Optional[str]:
        """Human-readable reason to fall back to CPU, or None if ready."""
        if not self.pcie_present:
            return "Hailo PCIe device not detected"
        if not self.cli_installed:
            return "hailortcli not installed"
        if not self.identify_ok:
            return self.error or "Hailo firmware did not respond to identify"
        return None


def _run(cmd: List[str], timeout: float = 5.0) -> subprocess.CompletedProcess:
    """Run a command, capturing stdout/stderr, never raising on non-zero exit."""
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _check_pcie(runner=_run) -> tuple[bool, list[str]]:
    """Return (present, list_of_matching_lspci_lines)."""
    if shutil.which("lspci") is None:
        return False, []
    try:
        result = runner(["lspci", "-nn"])
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.debug("lspci failed: %s", exc)
        return False, []
    matches = [
        line.strip()
        for line in result.stdout.splitlines()
        if _HAILO_PCI_KEYWORD in line or _HAILO_PCI_VENDOR_ID in line.lower()
    ]
    return bool(matches), matches


def _check_cli() -> tuple[bool, Optional[str]]:
    path = shutil.which("hailortcli")
    return path is not None, path


def _parse_identify(stdout: str) -> dict:
    """
    Parse `hailortcli fw-control identify` output.

    Sample lines (format may vary slightly between HailoRT versions):
        Board Name: Hailo-8
        Device Architecture: HAILO8
        Serial Number: HLDDLBB1234567890
        Firmware Version: 4.18.0 (release,app,extended context switch buffer)
    """
    fields = {}
    for line in stdout.splitlines():
        m = re.match(r"\s*([A-Za-z][A-Za-z0-9 _\-]*)\s*:\s*(.+?)\s*$", line)
        if not m:
            continue
        key = m.group(1).strip().lower().replace(" ", "_")
        fields[key] = m.group(2).strip()
    return fields


def _check_identify(runner=_run) -> tuple[bool, dict, Optional[str]]:
    """Return (ok, parsed_fields, error_message)."""
    if shutil.which("hailortcli") is None:
        return False, {}, "hailortcli not on PATH"
    try:
        result = runner(["hailortcli", "fw-control", "identify"], timeout=10.0)
    except subprocess.TimeoutExpired:
        return False, {}, "hailortcli identify timed out"
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip().splitlines()
        return False, {}, err[0] if err else f"exit {result.returncode}"
    fields = _parse_identify(result.stdout)
    if not fields:
        return False, {}, "could not parse hailortcli output"
    return True, fields, None


def probe(runner=_run) -> HailoStatus:
    """
    Run all checks and return a HailoStatus.

    Args:
        runner: callable matching `_run`'s signature; injectable for tests.

    Never raises — failures are reflected in HailoStatus.error.
    """
    status = HailoStatus()

    try:
        status.pcie_present, status.pcie_devices = _check_pcie(runner)
    except Exception as exc:
        status.error = f"pcie check failed: {exc}"
        return status

    status.cli_installed, status.cli_path = _check_cli()

    if not status.cli_installed:
        status.error = "hailortcli not installed (apt install hailo-all)"
        return status

    ok, fields, err = _check_identify(runner)
    status.identify_ok = ok
    if err:
        status.error = err

    status.board_name = fields.get("board_name")
    status.device_id = fields.get("device_architecture")
    status.serial_number = fields.get("serial_number")
    status.firmware_version = fields.get("firmware_version")

    return status
