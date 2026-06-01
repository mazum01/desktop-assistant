"""IoT plugin adapter for the EcoSense EcoQube radon monitor.

This device wraps ``RadonService`` as an ``IoTDevice`` so it renders
alongside other IoT devices in the Smart Home tab.  It is hardwired —
always auto-registered at startup and never persisted to
``config/iot_devices.json``.
"""

from __future__ import annotations

import collections
import time
from typing import Any, Optional

from src.iot.base import IoTDevice

_EPA_ACTION = 4.0    # pCi/L — EPA recommended action threshold
_SCALE_MAX  = 10.0   # pCi/L — sparkline ceiling (100 %)

_ALERT_COLORS = {
    "Green":   "#3fb950",
    "Orange":  "#d29922",
    "Red":     "#f85149",
    "Unknown": "#8b949e",
}


class RadonDevice(IoTDevice):
    """IoT adapter for the EcoSense EcoQube basement radon monitor."""

    device_id   = "radon"
    device_name = "Radon Monitor"
    device_icon = "☢️"
    _hardwired  = True  # always on, never user-removable via CRUD

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        super().__init__(bus=bus, cfg=cfg)
        self._svc: Optional[Any] = None
        self._history: collections.deque = collections.deque(maxlen=60)

    # ── IoTDevice interface ───────────────────────────────────────────────────

    def start(self) -> None:
        from src.services.radon_service import RadonService
        self._svc = RadonService(bus=self.bus, cfg=self._cfg)
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
                "EcoSense credentials not set — add ECOSENSE_USERNAME and "
                "ECOSENSE_PASSWORD to /etc/desktop-assistant/secrets.env"
            )

        reading = self._svc.get_reading()
        if not reading:
            return self._snapshot_unavailable("waiting for first reading")

        pcil  = reading.get("radon_pcil")
        bqm3  = reading.get("radon_bqm3")
        alert = reading.get("alert", "Unknown")
        color = _ALERT_COLORS.get(alert, _ALERT_COLORS["Unknown"])
        name  = reading.get("device_name", "EcoQube")
        err   = reading.get("error")

        if pcil is None:
            return self._snapshot_unavailable(err or "device initialising or offline")

        # Update sparkline history: normalise 0–10 pCi/L → 0–100
        self._history.append(round(min(pcil / _SCALE_MAX * 100, 100), 1))

        metrics = [{"label": "Bq/m³", "value": f"{bqm3:.1f}" if bqm3 else "—"}]
        if name:
            metrics.append({"label": "Device", "value": name})

        updated = reading.get("last_updated", "")
        if updated:
            updated = updated[:16].replace("T", " ")

        return {
            "available": True,
            "error":     None,
            "display": {
                "primary": {"value": f"{pcil:.2f}", "unit": "pCi/L", "color": color},
                "badges":  [{"text": alert, "color": color}],
                "metrics": metrics,
                "detail":  f"Updated: {updated}" if updated else "",
            },
            "history":       list(self._history),
            "history_label": "pCi/L (0–10 scale)",
        }

    # ── Custom announce ───────────────────────────────────────────────────────

    def announce(self) -> str:
        if self._svc is None or self._svc.degraded:
            return "The radon monitor credentials are not configured."
        reading = self._svc.get_reading()
        if not reading:
            return "The radon monitor has no reading yet."
        pcil  = reading.get("radon_pcil")
        alert = reading.get("alert", "Unknown")
        name  = reading.get("device_name", "EcoQube")
        if pcil is None:
            return f"{name} has no reading yet — the device may be initialising."
        if alert == "Green":
            return (
                f"The basement radon level is {pcil} picocuries per liter. "
                f"That's Green — well below the EPA action threshold."
            )
        if alert == "Orange":
            return (
                f"The basement radon level is {pcil} picocuries per liter. "
                f"That's Orange — the EPA recommends considering mitigation above 2.7."
            )
        return (
            f"Warning: basement radon level is {pcil} picocuries per liter. "
            f"That's Red — the EPA recommends fixing your home above 4 picocuries per liter."
        )
