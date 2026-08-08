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
    thermal = ThermalService(bus=bus)
    ipc = IPCBridge(
        bus=bus,
        pub_endpoint=THERMAL_PUB,
        rep_endpoint=THERMAL_REP,
    )

    def _get_manager():
        return getattr(thermal, "_manager", None)

    def _rpc_get_fan_control_points(_msg):
        manager = _get_manager()
        if manager is None:
            return {"ok": False, "error": "thermal manager unavailable"}
        return {"ok": True, "control_points": manager.get_control_points()}

    def _rpc_set_fan_control_points(msg):
        manager = _get_manager()
        if manager is None:
            return {"ok": False, "error": "thermal manager unavailable"}
        points = msg.get("points")
        if not isinstance(points, list) or len(points) < 2:
            return {"ok": False, "error": "points must contain at least two entries"}
        tuples: list[tuple[float, float]] = []
        for item in points:
            if isinstance(item, dict):
                if "temp_c" not in item or "duty" not in item:
                    return {"ok": False, "error": "each point must include temp_c and duty"}
                tuples.append((float(item["temp_c"]), float(item["duty"])))
                continue
            if isinstance(item, (list, tuple)) and len(item) == 2:
                tuples.append((float(item[0]), float(item[1])))
                continue
            return {"ok": False, "error": "invalid point format"}
        manager.set_control_points(tuples)
        return {"ok": True, "control_points": manager.get_control_points()}

    def _rpc_get_temp_blend(_msg):
        manager = _get_manager()
        if manager is None:
            return {"ok": False, "error": "thermal manager unavailable"}
        return {"ok": True, **manager.get_temp_blend()}

    def _rpc_set_temp_blend(msg):
        manager = _get_manager()
        if manager is None:
            return {"ok": False, "error": "thermal manager unavailable"}
        try:
            cw = float(msg.get("case_weight", 0.2))
            pw = float(msg.get("cpu_weight",  0.8))
            manager.set_temp_blend(cw, pw)
        except (TypeError, ValueError) as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, **manager.get_temp_blend()}

    ipc.register_rpc("fan_control_points.get", _rpc_get_fan_control_points)
    ipc.register_rpc("fan_control_points.set", _rpc_set_fan_control_points)
    ipc.register_rpc("temp_blend.get",         _rpc_get_temp_blend)
    ipc.register_rpc("temp_blend.set",         _rpc_set_temp_blend)
    ipc._all_services = [thermal, ipc]  # noqa: SLF001 - same-package wiring

    return run_services(
        services=[
            thermal,
            ipc,
        ],
        unit_name="thermal",
    )


if __name__ == "__main__":
    sys.exit(main())
