"""
Thermal entry point — safety-critical isolated process.

Owns the TMP117 sensor and PWM fan. Runs alone so a crash in any
other service cannot stop temperature monitoring. This is the unit
that systemd will restart most aggressively.

Run:
    python3 -m src.assistant.thermal_main

Or via systemd: services/systemd/desktop-assistant-thermal.service
"""

from __future__ import annotations

import sys

from src.assistant.runner import run_services
from src.core.bus import MessageBus
from src.services.ipc_bridge import IPCBridge
from src.services.thermal_service import ThermalService


# Separate IPC endpoints from the core process so both can run side by
# side. Core's IPCBridge connects a SUB to THERMAL_PUB and forwards every
# thermal.* event onto the core bus, where the CLI can see it.
THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"
THERMAL_REP = "ipc:///tmp/desktop-assistant-thermal.rep"


def main() -> int:
    bus = MessageBus()  # local bus — published events also fan out via IPCBridge
    return run_services(
        services=[
            ThermalService(bus=bus),
            IPCBridge(
                bus=bus,
                pub_endpoint=THERMAL_PUB,
                rep_endpoint=THERMAL_REP,
            ),
        ],
        unit_name="thermal",
    )


if __name__ == "__main__":
    sys.exit(main())
