"""
Service runner — shared boot/shutdown loop used by every entry point.

Each systemd unit (thermal, core, …) calls `run_services(...)` with the
list of `Service` instances it owns. Handles SIGINT/SIGTERM, ordered
startup, reverse-order shutdown, structured logging, and exit codes.

Each entry point is its own OS process and so has its own
`MessageBus` — there is no cross-process bus yet. When (if) we need
inter-process events, we'll add a transport layer here.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import List

from src.core.service import Service
from src.core.version import get_version

log = logging.getLogger("runner")


def run_services(services: List[Service], unit_name: str) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)-20s %(levelname)-7s %(message)s",
    )
    log.info("Desktop Assistant [%s] v%s — starting", unit_name, get_version())

    started: List[Service] = []
    for svc in services:
        try:
            svc.start()
            started.append(svc)
        except Exception:
            log.exception("Failed to start service %s", svc.name)

    if not started:
        log.error("No services started — exiting")
        return 1

    stopping = {"flag": False}

    def _shutdown(signum, _frame) -> None:
        log.info("Signal %d received — shutting down", signum)
        stopping["flag"] = True

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    log.info("[%s] %d service(s) running — entering main loop",
             unit_name, len(started))
    try:
        while not stopping["flag"]:
            time.sleep(0.5)
    finally:
        for svc in reversed(started):
            try:
                svc.stop()
            except Exception:
                log.exception("Error stopping %s", svc.name)

    log.info("[%s] exited cleanly", unit_name)
    return 0
