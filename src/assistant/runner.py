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
import threading
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

    # Boot self-test: a few seconds after services are up, sample telemetry
    # for obvious problems. If anything is red, ask the AV layer to speak.
    _run_boot_self_test(started, unit_name)

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


def _run_boot_self_test(started: List[Service], unit_name: str) -> None:
    """Wait briefly for first telemetry samples, then announce health."""
    if not started:
        return

    bus = started[0].bus

    def _check_after_grace():
        time.sleep(3.0)   # let services produce their first samples
        problems: List[str] = []

        # Service liveness — anything that didn't reach service.started is bad.
        for svc in started:
            if not svc.is_running():
                problems.append(f"{svc.name} did not start")

        # Topic-specific health
        thermal_err = bus.last("thermal.error")
        if thermal_err:
            problems.append(f"thermal error: {thermal_err}")

        temp = bus.last("thermal.temp") or {}
        if isinstance(temp, dict) and temp.get("ok") is False:
            problems.append("temperature sensor offline")

        vis_err = bus.last("vision.error")
        if vis_err:
            problems.append("vision subsystem error")

        aud_err = bus.last("audio.error")
        if aud_err:
            problems.append("audio capture error")

        if problems:
            log.warning("[%s] boot self-test found %d issue(s):", unit_name, len(problems))
            for p in problems:
                log.warning("  - %s", p)
            bus.publish("av.say", {
                "text": "Boot self test failed. " + "; ".join(problems),
            })
        else:
            log.info("[%s] boot self-test OK", unit_name)
            bus.publish("av.say", {"text": "All systems nominal."})

    threading.Thread(
        target=_check_after_grace,
        name="boot-self-test",
        daemon=True,
    ).start()
