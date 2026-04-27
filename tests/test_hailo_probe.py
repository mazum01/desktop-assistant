"""Unit tests for src/perception/hailo_probe.py."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from src.perception.hailo_probe import (
    HailoStatus,
    _parse_identify,
    probe,
)


# ---------------------------------------------------------------------------
# Sample outputs
# ---------------------------------------------------------------------------

LSPCI_PRESENT = """\
0000:01:00.0 Co-processor [0b40]: Hailo Technologies Ltd. Hailo-8 AI Processor [1e60:2864]
0000:00:00.0 PCI bridge [0604]: Broadcom Inc. and subsidiaries BCM2712 PCIe Bridge [1de4:0001]
"""

LSPCI_ABSENT = """\
0000:00:00.0 PCI bridge [0604]: Broadcom Inc. BCM2712 PCIe Bridge [1de4:0001]
0000:01:00.0 Non-Volatile memory [0108]: Realtek RTL9210B [10ec:5763]
"""

IDENTIFY_OK = """\
Identifying board
Control Protocol Version: 2
Firmware Version: 4.18.0 (release,app,extended context switch buffer)
Logger Version: 0
Board Name: Hailo-8
Device Architecture: HAILO8
Serial Number: HLDDLBB1234567890
Part Number: HM218B1C2FA
Product Name: HAILO-8 AI ACCELERATOR M.2 MODULE
"""

IDENTIFY_FAIL = "[HailoRT] [error] CHECK failed - Failed to open device\n"


# ---------------------------------------------------------------------------
# Fake runner helper
# ---------------------------------------------------------------------------

def make_runner(responses: dict[str, tuple[int, str, str]]):
    """
    Build a fake subprocess runner.

    `responses` maps the first command argument (e.g. "lspci", "hailortcli")
    to (returncode, stdout, stderr).
    """
    def runner(cmd, timeout=5.0):
        key = cmd[0]
        if key not in responses:
            raise FileNotFoundError(key)
        rc, out, err = responses[key]
        return subprocess.CompletedProcess(cmd, rc, out, err)
    return runner


# ---------------------------------------------------------------------------
# Tests — _parse_identify
# ---------------------------------------------------------------------------

class TestParseIdentify:
    def test_parses_board_name(self):
        f = _parse_identify(IDENTIFY_OK)
        assert f["board_name"] == "Hailo-8"

    def test_parses_serial(self):
        f = _parse_identify(IDENTIFY_OK)
        assert f["serial_number"] == "HLDDLBB1234567890"

    def test_parses_firmware_version(self):
        f = _parse_identify(IDENTIFY_OK)
        assert f["firmware_version"].startswith("4.18.0")

    def test_parses_architecture(self):
        f = _parse_identify(IDENTIFY_OK)
        assert f["device_architecture"] == "HAILO8"

    def test_empty_input_returns_empty_dict(self):
        assert _parse_identify("") == {}


# ---------------------------------------------------------------------------
# Tests — probe()
# ---------------------------------------------------------------------------

class TestProbeFullyReady:
    def test_all_green(self):
        runner = make_runner({
            "lspci": (0, LSPCI_PRESENT, ""),
            "hailortcli": (0, IDENTIFY_OK, ""),
        })
        with patch("src.perception.hailo_probe.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            status = probe(runner=runner)
        assert status.fully_ready is True
        assert status.pcie_present is True
        assert status.cli_installed is True
        assert status.identify_ok is True
        assert status.board_name == "Hailo-8"
        assert status.serial_number == "HLDDLBB1234567890"
        assert status.firmware_version.startswith("4.18.0")
        assert status.degrade_reason() is None


class TestProbePcieMissing:
    def test_no_hailo_in_lspci(self):
        runner = make_runner({
            "lspci": (0, LSPCI_ABSENT, ""),
            "hailortcli": (0, IDENTIFY_OK, ""),
        })
        with patch("src.perception.hailo_probe.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            status = probe(runner=runner)
        assert status.pcie_present is False
        assert status.fully_ready is False
        assert "PCIe" in status.degrade_reason()


class TestProbeCliMissing:
    def test_hailortcli_not_on_path(self):
        runner = make_runner({"lspci": (0, LSPCI_PRESENT, "")})
        with patch("src.perception.hailo_probe.shutil.which",
                   side_effect=lambda c: "/usr/bin/lspci" if c == "lspci" else None):
            status = probe(runner=runner)
        assert status.cli_installed is False
        assert status.fully_ready is False
        assert "hailortcli" in status.degrade_reason()


class TestProbeIdentifyFails:
    def test_firmware_unresponsive(self):
        runner = make_runner({
            "lspci": (0, LSPCI_PRESENT, ""),
            "hailortcli": (1, "", IDENTIFY_FAIL),
        })
        with patch("src.perception.hailo_probe.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            status = probe(runner=runner)
        assert status.pcie_present is True
        assert status.cli_installed is True
        assert status.identify_ok is False
        assert status.fully_ready is False
        assert status.degrade_reason() is not None


class TestProbeNeverRaises:
    def test_runner_throws_filenotfound(self):
        def runner(cmd, timeout=5.0):
            raise FileNotFoundError(cmd[0])
        with patch("src.perception.hailo_probe.shutil.which", return_value=None):
            status = probe(runner=runner)
        assert isinstance(status, HailoStatus)
        assert status.fully_ready is False


class TestProbeTimeout:
    def test_identify_timeout_handled(self):
        def runner(cmd, timeout=5.0):
            if cmd[0] == "hailortcli":
                raise subprocess.TimeoutExpired(cmd, timeout)
            return subprocess.CompletedProcess(cmd, 0, LSPCI_PRESENT, "")

        with patch("src.perception.hailo_probe.shutil.which",
                   side_effect=lambda c: f"/usr/bin/{c}"):
            status = probe(runner=runner)
        assert status.identify_ok is False
        assert "timed out" in status.error.lower()


class TestHailoStatusDataclass:
    def test_default_not_ready(self):
        s = HailoStatus()
        assert s.fully_ready is False
        assert s.degrade_reason() is not None

    def test_degrade_reason_when_ready(self):
        s = HailoStatus(
            pcie_present=True,
            cli_installed=True,
            identify_ok=True,
        )
        assert s.degrade_reason() is None
