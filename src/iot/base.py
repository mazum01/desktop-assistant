"""IoTDevice — abstract base class for all VERA IoT plugin devices."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class IoTDevice(ABC):
    """Abstract base class for all VERA IoT device plugins.

    Subclasses must define ``device_id`` and ``device_name`` as class-level
    string attributes and implement ``start()``, ``stop()``, and
    ``get_snapshot()``.

    **Snapshot schema** returned by ``get_snapshot()``::

        {
            "available": bool,        # False if device unavailable/errored
            "error": str | None,      # Error message when available=False
            "display": {
                "primary": {
                    "value": str,     # e.g. "22.5"
                    "unit":  str,     # e.g. "°C"
                    "color": str,     # CSS hex color, e.g. "#58a6ff"
                },
                "badges": [           # Status pills shown next to primary
                    {"text": str, "color": str},
                ],
                "metrics": [          # Secondary key-value rows
                    {"label": str, "value": str},
                ],
                "detail": str,        # Optional single detail line
            },
            "history":       list[float],  # 0–100 normalized, newest last
            "history_label": str,          # e.g. "Flow rate (gpm)"
        }
    """

    # ── Required class-level attributes ──────────────────────────────────────
    device_id:   str = ""   # Unique slug, e.g. "soil_moisture"
    device_name: str = ""   # Display name, e.g. "Soil Moisture"
    device_icon: str = "🔌" # Emoji shown in card header

    # ── Optional class-level flags ────────────────────────────────────────────
    _hardwired: bool = False
    """When True the device is always registered by core_main at startup,
    never saved to ``config/iot_devices.json``, and excluded from the
    ``/api/iot/types`` discovery list (so it cannot be double-added via the UI).
    Radon and DROP are hardwired because they are always present on VERA.
    """

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        self.bus   = bus
        self._cfg  = cfg or {}

    # ── Required interface ────────────────────────────────────────────────────

    @abstractmethod
    def start(self) -> None:
        """Start the device (connect, spawn threads, subscribe to bus)."""

    @abstractmethod
    def stop(self) -> None:
        """Stop the device cleanly."""

    @abstractmethod
    def get_snapshot(self) -> dict[str, Any]:
        """Return a display-ready snapshot dict (see class docstring schema)."""

    # ── Optional overrides ───────────────────────────────────────────────────

    def execute_action(self, action: str, params: dict | None = None) -> dict:
        """Execute a device-specific action (e.g. lock, unlock, open, close).

        Returns a dict with at least ``{"ok": bool, "message": str}``.
        The default implementation returns an error; subclasses override this.

        *params* is an optional dict of action-specific parameters (e.g.
        ``{"pin": "1234"}`` for an unlock action).
        """
        return {"ok": False, "message": f"Action '{action}' not supported by {self.device_name}"}

    def get_actions(self) -> list[dict]:
        """Return a list of available actions for this device.

        Each action dict has:
          ``id``          (str)  — action identifier, passed to ``execute_action()``
          ``label``       (str)  — human-readable label shown on the button
          ``icon``        (str)  — emoji icon for the button
          ``color``       (str)  — CSS hex color for the button (optional)
          ``requires_pin``(bool) — whether a PIN prompt is needed before calling (optional)

        The default returns an empty list (no actions).
        """
        return []

    def announce(self) -> str:
        """Return a TTS-friendly status string for this device.

        The default implementation reads the primary display value.
        Subclasses can override for richer announcements.
        """
        snap = self.get_snapshot()
        if not snap.get("available"):
            err = snap.get("error") or "unavailable"
            return f"{self.device_name} is {err}."
        disp    = snap.get("display") or {}
        primary = disp.get("primary") or {}
        value   = primary.get("value", "unknown")
        unit    = primary.get("unit", "")
        badges  = disp.get("badges") or []
        metrics = disp.get("metrics") or []

        parts = [f"{self.device_name}: {value}{(' ' + unit) if unit else ''}"]
        for badge in badges:
            parts.append(badge.get("text", ""))
        for m in metrics[:2]:
            parts.append(f"{m['label']}: {m['value']}")
        return ". ".join(p for p in parts if p) + "."

    # ── Snapshot helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _snapshot_unavailable(error: str = "not available") -> dict[str, Any]:
        """Convenience: return a canonical unavailable snapshot."""
        return {
            "available": False,
            "error":     error,
            "display":   {"primary": {}, "badges": [], "metrics": [], "detail": ""},
            "history":       [],
            "history_label": "",
        }
