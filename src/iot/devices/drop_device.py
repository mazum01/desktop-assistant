"""IoT plugin adapter for the DROP local MQTT water softener system.

This device wraps ``DropService`` as an ``IoTDevice`` so it renders
alongside other IoT devices in the Smart Home tab.  It is hardwired —
always auto-registered at startup and never persisted to
``config/iot_devices.json``.
"""

from __future__ import annotations

import collections
from typing import Any, Optional

from src.iot.base import IoTDevice

_FLOW_SCALE_MAX = 5.0   # gpm — sparkline ceiling (100 %)


class DropDevice(IoTDevice):
    """IoT adapter for the DROP Hub MQTT water softener system."""

    device_id   = "drop"
    device_name = "Water Softener"
    device_icon = "💧"
    _hardwired  = True  # always on, never user-removable via CRUD

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        super().__init__(bus=bus, cfg=cfg)
        self._svc: Optional[Any] = None
        self._history: collections.deque = collections.deque(maxlen=60)

    # ── IoTDevice interface ───────────────────────────────────────────────────

    def start(self) -> None:
        from src.services.drop_service import DropService
        self._svc = DropService(bus=self.bus, cfg=self._cfg)
        self._svc.start()

    def stop(self) -> None:
        if self._svc is not None:
            self._svc.stop()
            self._svc = None

    def get_snapshot(self) -> dict[str, Any]:
        if self._svc is None:
            return self._snapshot_unavailable("service not started")

        if self._svc.degraded:
            return self._snapshot_unavailable(
                "MQTT broker unreachable or dropmqttapi not installed"
            )

        reading = self._svc.get_reading()
        if not reading:
            return self._snapshot_unavailable("waiting for MQTT data from DROP Hub")

        flow     = reading.get("flow_gpm")
        used     = reading.get("used_today_gal")
        capacity = reading.get("capacity_remaining_gal")
        pressure = reading.get("pressure_psi")
        salt_low = reading.get("salt_low")
        water_on = reading.get("water_on")
        bypass   = reading.get("bypass_on", False)
        mode     = reading.get("protect_mode", "home")

        # Primary: flow rate
        flow_val = flow if flow is not None else 0.0
        self._history.append(round(min(flow_val / _FLOW_SCALE_MAX * 100, 100), 1))

        # Status badge
        if water_on is False:
            badge_text  = "Water OFF"
            badge_color = "#f85149"
        elif bypass:
            badge_text  = "Bypass"
            badge_color = "#d29922"
        elif mode == "away":
            badge_text  = "Away"
            badge_color = "#58a6ff"
        elif mode == "vacation":
            badge_text  = "Vacation"
            badge_color = "#58a6ff"
        else:
            badge_text  = "Protecting"
            badge_color = "#3fb950"

        badges = [{"text": badge_text, "color": badge_color}]
        if salt_low is True:
            badges.append({"text": "⚠ Salt LOW", "color": "#f85149"})
        elif salt_low is False:
            badges.append({"text": "Salt OK", "color": "#3fb950"})

        metrics = []
        if used is not None:
            metrics.append({"label": "Used today", "value": f"{used:.1f} gal"})
        if capacity is not None:
            metrics.append({"label": "Capacity left", "value": f"{capacity:.0f} gal"})
        if pressure is not None:
            metrics.append({"label": "Pressure", "value": f"{pressure:.0f} PSI"})
        if reading.get("peak_flow_gpm") is not None:
            metrics.append({"label": "Peak flow", "value": f"{reading['peak_flow_gpm']:.1f} gpm"})
        if reading.get("avg_used_gal") is not None:
            metrics.append({"label": "Avg/day", "value": f"{reading['avg_used_gal']:.0f} gal"})
        tds_in  = reading.get("tds_in_ppm")
        tds_out = reading.get("tds_out_ppm")
        if tds_in is not None:
            metrics.append({"label": "TDS in",  "value": f"{tds_in} ppm"})
        if tds_out is not None:
            metrics.append({"label": "TDS out", "value": f"{tds_out} ppm"})

        return {
            "available": True,
            "error":     None,
            "display": {
                "primary": {
                    "value": f"{flow_val:.2f}",
                    "unit":  "gpm",
                    "color": "#58a6ff",
                },
                "badges":  badges,
                "metrics": metrics,
                "detail":  "",
            },
            "history":       list(self._history),
            "history_label": "Flow rate (gpm, 0–5 scale)",
        }

    # ── Custom announce ───────────────────────────────────────────────────────

    def announce(self) -> str:
        if self._svc is None or self._svc.degraded:
            return "The DROP water softener is not connected."
        reading = self._svc.get_reading()
        if not reading:
            return "The DROP water softener has no data yet."

        parts: list[str] = []
        name = reading.get("softener_name", "water softener")
        flow = reading.get("flow_gpm")
        if flow is not None:
            parts.append(f"current flow is {flow:.1f} gallons per minute")
        used = reading.get("used_today_gal")
        if used is not None:
            parts.append(f"{used:.0f} gallons used today")
        capacity = reading.get("capacity_remaining_gal")
        if capacity is not None:
            parts.append(f"{capacity:.0f} gallons of softener capacity remaining")
        pressure = reading.get("pressure_psi")
        if pressure is not None:
            parts.append(f"system pressure is {pressure:.0f} PSI")
        if reading.get("salt_low"):
            parts.append("salt level is LOW — add salt to the brine tank soon")
        if reading.get("water_on") is False:
            parts.append("WARNING: water supply is shut off")

        if parts:
            return f"DROP {name} status: {'; '.join(parts)}."
        return f"The DROP {name} is connected but no readings are available yet."
