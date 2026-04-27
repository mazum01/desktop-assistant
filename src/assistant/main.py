"""
Top-level assistant boot entry point.

Starts the core services on the shared message bus, announces the
version through TTS, then idles until SIGINT/SIGTERM.

Run with:
    python3 -m src.assistant.main

Or via systemd: see services/systemd/desktop-assistant.service
"""

from __future__ import annotations

import logging
import signal
import sys
import time

from src.core.bus import default_bus
from src.core.version import get_version
from src.services.av_service import AVService
from src.services.motion_service import MotionService
from src.services.thermal_service import ThermalService

log = logging.getLogger("assistant")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    )
    log.info("Desktop Assistant v%s — starting", get_version())

    bus = default_bus()
    services = [
        ThermalService(bus=bus),
        MotionService(bus=bus),
        AVService(bus=bus),
    ]

    for svc in services:
        try:
            svc.start()
        except Exception:
            log.exception("Failed to start %s", svc.name)

    # Graceful shutdown
    stopping = {"flag": False}

    def _shutdown(signum, _frame) -> None:
        log.info("Signal %d received — shutting down", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("All services started — entering main loop")
    try:
        while not stopping["flag"]:
            time.sleep(0.5)
    finally:
        for svc in reversed(services):
            try:
                svc.stop()
            except Exception:
                log.exception("Error stopping %s", svc.name)

    log.info("Desktop Assistant exited cleanly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
