"""
Core entry point — motion, AV, and (later) perception/dialog.

Runs in a single process with an in-process MessageBus. Crashes here
do NOT affect the thermal-safety unit.

Run:
    python3 -m src.assistant.core_main

Or via systemd: services/systemd/desktop-assistant-core.service
"""

from __future__ import annotations

import sys

from src.assistant.runner import run_services
from src.core.bus import MessageBus
from src.services.av_service import AVService
from src.services.motion_service import MotionService


def main() -> int:
    bus = MessageBus()
    return run_services(
        services=[
            MotionService(bus=bus),
            AVService(bus=bus),
        ],
        unit_name="core",
    )


if __name__ == "__main__":
    sys.exit(main())
