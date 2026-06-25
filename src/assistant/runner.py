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
    log.info("VERA [%s] v%s — starting", unit_name, get_version())

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
                status_lines.append(f"{label} failed to start")
            elif hw is False:
                status_lines.append(f"{label} not connected")
            else:
                status_lines.append(f"{label} ready")

        # ── Topic-specific health ────────────────────────────────────────
        thermal_err = bus.last("thermal.error")
        if thermal_err:
            problems.append(f"thermal error: {thermal_err}")

        # Temperature: try bus first (populated when thermal process is up),
        # then fall back to a direct TMP117 read so the core unit always has
        # a value at boot even when running standalone.
        temp_bus = bus.last("thermal.temp") or {}
        case_c: float | None = None
        soc_c: float | None = None
        blended_c: float | None = None
        if isinstance(temp_bus, dict) and temp_bus.get("ok") is False:
            problems.append("temperature sensor offline")
            status_lines.append("Temperature sensor not responding")
        else:
            if isinstance(temp_bus, dict):
                case_c = temp_bus.get("case_celsius")
                soc_c = temp_bus.get("cpu_celsius")
                blended_c = temp_bus.get("blended_celsius")
                if blended_c is None:
                    blended_c = temp_bus.get("celsius")
            if blended_c is None:
                # Thermal unit not running or IPC bridge hasn't forwarded yet —
                # read the sensor directly.
                try:
                    from src.thermal.tmp117 import TMP117
                    _sensor = TMP117()
                    blended_c = _sensor.read_temperature_c()
                    _sensor.close()
                except Exception:
                    blended_c = None

            if case_c is not None:
                status_lines.append(
                    f"Case temperature at {(case_c * 9.0 / 5.0 + 32.0):.0f} degrees"
                )
            if soc_c is not None:
                status_lines.append(
                    f"SoC temperature at {(soc_c * 9.0 / 5.0 + 32.0):.0f} degrees"
                )
            if blended_c is not None:
                status_lines.append(
                    f"Blended temperature at {(blended_c * 9.0 / 5.0 + 32.0):.0f} degrees"
                )
            else:
                status_lines.append("Temperature sensor not connected")

        # Fan: probe sysfs/lgpio non-destructively (avoids conflicting with
        # the thermal process which may own the FanController).
        try:
            from pathlib import Path as _Path
            _chip = _Path("/sys/class/pwm/pwmchip0")
            if _chip.is_dir():
                fan_line = "Fan control ready"
            else:
                try:
                    import lgpio as _lgpio  # noqa: F401
                    fan_line = "Fan control running in software mode"
                except ImportError:
                    fan_line = "Fan control not connected"
        except Exception:
            fan_line = "Fan control not connected"
        status_lines.append(fan_line)

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
                bus.publish("av.say", {"text": "Boot self test failed. " + ". ".join(problems)})
            else:
                log.info("[%s] boot self-test OK", unit_name)
                bus.publish("av.say", {"text": "All systems nominal. How may I help?"})
            return

        # Core unit — full spoken readout.
        # Comma-separated items give Piper a short breath between each; the
        # ellipsis before the greeting creates a clear audible break before
        # switching tone from status report to conversational greeting.
        readout = "Running diagnostics. " + ", ".join(status_lines) + " ... "

        if problems:
            log.warning("[%s] boot self-test found %d issue(s):", unit_name, len(problems))
            for p in problems:
                log.warning("  - %s", p)
            readout += "Warning. " + ". ".join(problems) + "."
            bus.publish("av.say", {"text": readout})
        else:
            log.info("[%s] boot self-test OK", unit_name)
            greeting = _time_of_day_greeting()
            readout += f"{greeting}. I'm ready. How may I help?"
            bus.publish("av.say", {"text": readout})

    threading.Thread(
        target=_check_after_grace,
        name="boot-self-test",
        daemon=True,
    ).start()
