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
from src.services.thermal_service import ThermalService


def main() -> int:
    bus = MessageBus()  # local bus — this process is self-contained
    return run_services(
        services=[ThermalService(bus=bus)],
        unit_name="thermal",
    )


if __name__ == "__main__":
    sys.exit(main())
