#!/usr/bin/env python3
"""
Hailo-8 bring-up probe — verifies the AI accelerator is detected,
the runtime is installed, and the firmware responds.

Run on the Pi:
    python3 scripts/test_hailo.py

Exits 0 if the Hailo is fully ready, 1 if it's missing or unresponsive.
The assistant gracefully degrades to CPU when this fails — failure is
not fatal to the project, but you should know about it.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.perception.hailo_probe import probe

print("╔══════════════════════════════════════════════════╗")
print("║    Desktop Assistant — Hailo-8 bring-up probe   ║")
print("╚══════════════════════════════════════════════════╝")
print()

status = probe()

# ── PCIe ───────────────────────────────────────────────────────────────
print("── PCIe presence ──")
if status.pcie_present:
    for line in status.pcie_devices:
        print(f"  ✓ {line}")
else:
    print("  ✗ No Hailo device found in `lspci`.")
    print("    Check:")
    print("      1. AI HAT+ seated on the Pi 5 PCIe connector")
    print("      2. /boot/firmware/config.txt has `dtparam=pciex1` (and reboot)")
    print("      3. AI HAT+ getting-started guide:")
    print("         https://www.raspberrypi.com/documentation/accessories/ai-kit.html")

# ── CLI ────────────────────────────────────────────────────────────────
print()
print("── HailoRT CLI ──")
if status.cli_installed:
    print(f"  ✓ hailortcli at {status.cli_path}")
else:
    print("  ✗ hailortcli not installed.")
    print("    Install with:")
    print("      sudo apt-get install -y hailo-all")
    print("    Then reboot.")

# ── Identify ───────────────────────────────────────────────────────────
print()
print("── Firmware identify ──")
if status.identify_ok:
    print(f"  ✓ Board:    {status.board_name}")
    print(f"  ✓ Arch:     {status.device_id}")
    print(f"  ✓ Serial:   {status.serial_number}")
    print(f"  ✓ Firmware: {status.firmware_version}")
else:
    print(f"  ✗ Identify failed: {status.error}")

# ── Verdict ────────────────────────────────────────────────────────────
print()
print("══════════════════════════════════════════════════")
if status.fully_ready:
    print("Hailo-8 ready ✓ — Phase 3 inference can use the accelerator.")
    sys.exit(0)
else:
    print(f"Hailo-8 NOT ready: {status.degrade_reason()}")
    print("Project will fall back to CPU inference (per safety imperative).")
    sys.exit(1)
