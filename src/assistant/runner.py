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
from datetime import datetime
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


def _time_of_day_greeting() -> str:
    """Return 'Good morning', 'Good afternoon', or 'Good evening'."""
    hour = datetime.now().hour
    if hour < 12:
        return "Good morning"
    if hour < 17:
        return "Good afternoon"
    return "Good evening"


def _run_boot_self_test(started: List[Service], unit_name: str) -> None:
    """Wait briefly for first telemetry samples, then announce health."""
    if not started:
        return

    bus = started[0].bus

    # Human-friendly labels for service names
    _SERVICE_LABELS = {
        "motion":        "Motion system",
        "vision":        "Camera",
        "audio_capture": "Microphone",
        "av":            "Audio output",
        "perception":    "Face detection",
        "telemetry":     "Telemetry",
        "thermal":       "Thermal management",
        "ipc_bridge":    "Communications bridge",
    }

    def _check_after_grace():
        time.sleep(3.0)   # let services produce their first samples
        problems: List[str] = []
        status_lines: List[str] = []

        # ── Service liveness ────────────────────────────────────────────
        for svc in started:
            label = _SERVICE_LABELS.get(svc.name, svc.name)
            running = svc.is_running()
            if not running:
                problems.append(f"{svc.name} did not start")

            # Check hardware_ready if the service exposes it
            hw = getattr(svc, "hardware_ready", None)
            if not running:
                status_lines.append(f"{label}: failed to start")
            elif hw is False:
                status_lines.append(f"{label}: online, simulation mode")
            else:
                status_lines.append(f"{label}: online")

        # ── Topic-specific health ────────────────────────────────────────
        thermal_err = bus.last("thermal.error")
        if thermal_err:
            problems.append(f"thermal error: {thermal_err}")

        temp = bus.last("thermal.temp") or {}
        if isinstance(temp, dict) and temp.get("ok") is False:
            problems.append("temperature sensor offline")
            status_lines.append("Temperature sensor: offline")
        else:
            temp_c = temp.get("temp_c") if isinstance(temp, dict) else None
            if temp_c is not None:
                status_lines.append(f"Temperature sensor: online, {temp_c:.1f} degrees")
            else:
                status_lines.append("Temperature sensor: no reading yet")

        vis_err = bus.last("vision.error")
        if vis_err:
            problems.append("vision subsystem error")

        aud_err = bus.last("audio.error")
        if aud_err:
            problems.append("audio capture error")

        # ── Announce results ─────────────────────────────────────────────
        if unit_name != "core":
            if problems:
                log.warning("[%s] boot self-test found %d issue(s):", unit_name, len(problems))
                for p in problems:
                    log.warning("  - %s", p)
                bus.publish("av.say", {"text": "Boot self test failed. " + "; ".join(problems)})
            else:
                log.info("[%s] boot self-test OK", unit_name)
                bus.publish("av.say", {"text": "All systems nominal."})
            return

        # Core unit — full spoken readout
        readout = "Running diagnostics. " + ". ".join(status_lines) + ". "

        if problems:
            log.warning("[%s] boot self-test found %d issue(s):", unit_name, len(problems))
            for p in problems:
                log.warning("  - %s", p)
            readout += "Warning: " + "; ".join(problems) + "."
            bus.publish("av.say", {"text": readout})
        else:
            log.info("[%s] boot self-test OK", unit_name)
            greeting = _time_of_day_greeting()
            readout += f"{greeting}. I'm ready."
            bus.publish("av.say", {"text": readout})

    threading.Thread(
        target=_check_after_grace,
        name="boot-self-test",
        daemon=True,
    ).start()
