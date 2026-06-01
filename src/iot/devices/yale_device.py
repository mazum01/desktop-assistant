"""Yale Smart Lock IoT device plugin.

Config keys expected in ``config``:
    username        Yale / August app e-mail address (required).
    password        Yale / August app password (required).
    lock_name       Name of the lock to target (optional; first lock if omitted).
    unlock_pin      Default PIN used when none is supplied to the unlock action.
    poll_interval   Polling interval in seconds (default: 30).
"""

from __future__ import annotations

import logging
from typing import Any

from ..base import IoTDevice

log = logging.getLogger(__name__)

_STATE_COLORS = {
    "locked":    "#4caf50",   # green
    "unlocked":  "#f44336",   # red
    "door_open": "#ff9800",   # amber
    "unknown":   "#9e9e9e",   # grey
}

_STATE_ICONS = {
    "locked":    "🔒",
    "unlocked":  "🔓",
    "door_open": "🚪",
    "unknown":   "❓",
}

_STATE_LABELS = {
    "locked":    "Locked",
    "unlocked":  "Unlocked",
    "door_open": "Door Open",
    "unknown":   "Unknown",
}


class YaleDevice(IoTDevice):
    """IoT plugin for Yale WiFi smart locks via the August/Yale cloud API."""

    device_id = "yale_lock"
    device_name = "Yale Lock"
    device_icon = "🔒"
    _hardwired = False

    def __init__(self, bus: Any = None, config: dict | None = None) -> None:
        self.bus = bus
        self._config = config or {}
        self._svc: Any = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from ...services.yale_service import YaleService
        self._svc = YaleService(bus=self.bus, cfg=self._config)
        self._svc.start()
        log.info("YaleDevice: started")

    def stop(self) -> None:
        if self._svc:
            self._svc.stop()
        log.info("YaleDevice: stopped")

    # ── IoTDevice API ─────────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        if self._svc is None or self._svc.degraded:
            reason = getattr(self._svc, "_degraded_reason", "Service not available") if self._svc else "Service not started"
            return self._snapshot_unavailable(reason)

        reading = self._svc.get_reading()
        if reading is None:
            return self._snapshot_unavailable("Waiting for first lock state poll…")

        state = reading.get("state", "unknown")
        label = _STATE_LABELS.get(state, state.title())
        color = _STATE_COLORS.get(state, "#9e9e9e")
        icon  = _STATE_ICONS.get(state, "❓")
        name  = reading.get("name", "Lock")

        metrics = []
        autolock = reading.get("autolock")
        if autolock is not None:
            metrics.append({"label": "Auto-lock", "value": "On" if autolock else "Off"})

        return {
            "available": True,
            "error": None,
            "display": {
                "primary":  {"value": f"{icon} {label}", "unit": name, "color": color},
                "badges":   [{"text": label, "color": color}],
                "metrics":  metrics,
                "detail":   f"{name} is {label.lower()}.",
            },
            "history":       [],
            "history_label": "Lock state",
            "actions":       self.get_actions(),
        }

    def get_actions(self) -> list[dict]:
        return [
            {
                "id":    "lock",
                "label": "Lock",
                "icon":  "🔒",
                "color": "#4caf50",
            },
            {
                "id":           "unlock",
                "label":        "Unlock",
                "icon":         "🔓",
                "color":        "#f44336",
                "requires_pin": True,
            },
        ]

    def execute_action(self, action: str, params: dict | None = None) -> dict:
        if self._svc is None or self._svc.degraded:
            reason = getattr(self._svc, "_degraded_reason", "Service unavailable") if self._svc else "Service not started"
            return {"ok": False, "message": reason}

        p = params or {}

        if action == "lock":
            ok, msg = self._svc.lock()
            return {"ok": ok, "message": msg if not ok else "Locked successfully"}

        if action == "unlock":
            pin = str(p.get("pin", ""))
            ok, msg = self._svc.unlock(pin=pin)
            return {"ok": ok, "message": msg if not ok else "Unlocked successfully"}

        return {"ok": False, "message": f"Unknown action '{action}'"}

    def announce(self) -> str:
        if self._svc is None or self._svc.degraded:
            return "Yale lock status is unavailable."

        reading = self._svc.get_reading()
        if reading is None:
            return "Yale lock status is not yet available."

        name  = reading.get("name", "The lock")
        state = reading.get("state", "unknown")
        label = _STATE_LABELS.get(state, state)

        text = f"{name} is {label.lower()}."
        if self.bus:
            self.bus.publish("av.say", {"text": text})
        return text
