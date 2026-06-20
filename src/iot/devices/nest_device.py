"""Nest thermostat IoT device plugin.

Wraps NestService and presents a live thermostat card in the VERA web GUI.

Config keys (passed via web GUI or ``vera iot config nest_thermostat …``):
    project_id      Google Device Access project ID.
    client_id       Google Cloud OAuth2 client ID.
    client_secret   Google Cloud OAuth2 client secret (stored secret).
    refresh_token   OAuth2 long-lived refresh token (stored secret).
    device_id       SDM device resource name (optional; auto-selects first thermostat).
    poll_interval   Polling interval in seconds (default: 60).

Display:
  Primary:  Current ambient temperature in °F.
  Badges:   HVAC status (HEATING / COOLING / IDLE) + mode (HEAT / COOL / ECO / OFF).
  Metrics:  Set-point(s), humidity, eco set-points when active.

Actions:
  set_heat    Prompt for target heat temperature in °F.
  set_cool    Prompt for target cool temperature in °F.
  mode_heat   Switch to HEAT mode.
  mode_cool   Switch to COOL mode.
  mode_range  Switch to HEAT+COOL (range) mode.
  mode_eco    Enable eco mode.
  mode_off    Turn off HVAC.
"""

from __future__ import annotations

import logging
from typing import Any

from ..base import IoTDevice

log = logging.getLogger(__name__)

# HVAC status → display
_HVAC_COLORS = {
    "HEATING": "#f44336",   # red
    "COOLING": "#2196f3",   # blue
    "OFF":     "#4caf50",   # green
}
_HVAC_ICONS = {
    "HEATING": "🔥",
    "COOLING": "❄️",
    "OFF":     "✅",
}
_HVAC_LABELS = {
    "HEATING": "Heating",
    "COOLING": "Cooling",
    "OFF":     "Idle",
}

_MODE_ICONS = {
    "HEAT":       "🔆",
    "COOL":       "🧊",
    "HEATCOOL":   "↕️",
    "MANUAL_ECO": "🌿",
    "OFF":        "⏹️",
    "UNKNOWN":    "❓",
}

_TEMP_MIN_F = 40.0
_TEMP_MAX_F = 95.0


class NestDevice(IoTDevice):
    """IoT plugin for Google Nest thermostat via the SDM API."""

    device_id   = "nest_thermostat"
    device_name = "Nest Thermostat"
    device_icon = "🌡️"
    _hardwired  = False

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        self.bus  = bus
        self._cfg = cfg or {}
        self._svc: Any = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        from ...services.nest_service import NestService
        self._svc = NestService(bus=self.bus, cfg=self._cfg)
        self._svc.start()
        log.info("NestDevice: started")

    def stop(self) -> None:
        if self._svc:
            self._svc.stop()
        log.info("NestDevice: stopped")

    # ── IoTDevice API ─────────────────────────────────────────────────────────

    def get_snapshot(self) -> dict:
        if self._svc is None:
            return self._snapshot_unavailable("Service not started")
        if self._svc.degraded:
            reason = getattr(self._svc, "_degraded_reason", "Service unavailable")
            return self._snapshot_unavailable(reason)

        reading = self._svc.get_reading()
        if reading is None:
            return self._snapshot_unavailable("Waiting for first thermostat poll…")

        temp_f    = reading.get("temp_f")
        humidity  = reading.get("humidity")
        mode      = reading.get("mode", "UNKNOWN")
        avail     = reading.get("available_modes", [])
        hvac      = reading.get("hvac_status", "OFF")
        heat_f    = reading.get("heat_f")
        cool_f    = reading.get("cool_f")
        eco_mode  = reading.get("eco_mode", "OFF")
        eco_heat  = reading.get("eco_heat_f")
        eco_cool  = reading.get("eco_cool_f")
        conn      = reading.get("connectivity", "ONLINE")
        fan_mode  = reading.get("fan_mode", "OFF")
        custom_nm = reading.get("custom_name", "")
        dev_name  = custom_nm or reading.get("device_name", "Thermostat")

        primary_value = f"{temp_f:.1f}°F" if temp_f is not None else "—"
        primary_color = _HVAC_COLORS.get(hvac, "#9e9e9e")

        hvac_label  = _HVAC_LABELS.get(hvac, hvac.title())
        hvac_color  = _HVAC_COLORS.get(hvac, "#9e9e9e")
        mode_icon   = _MODE_ICONS.get(mode, "❓")
        mode_label  = mode.replace("_", " ").title()
        badges = []
        if conn != "ONLINE":
            badges.append({"text": "⚠️ Offline", "color": "#ff5722"})
        badges.append({"text": f"{_HVAC_ICONS.get(hvac, '')} {hvac_label}", "color": hvac_color})
        badges.append({"text": f"{mode_icon} {mode_label}", "color": "#9e9e9e"})
        if eco_mode == "MANUAL_ECO":
            badges.append({"text": "🌿 Eco", "color": "#4caf50"})
        if fan_mode not in ("OFF", "", None):
            badges.append({"text": "💨 Fan", "color": "#00bcd4"})

        metrics: list[dict] = []
        if mode in ("HEAT", "HEATCOOL") and heat_f is not None:
            metrics.append({"label": "Heat to", "value": f"{heat_f:.1f}°F"})
        if mode in ("COOL", "HEATCOOL") and cool_f is not None:
            metrics.append({"label": "Cool to", "value": f"{cool_f:.1f}°F"})
        if eco_mode == "MANUAL_ECO":
            if eco_heat is not None:
                metrics.append({"label": "Eco heat", "value": f"{eco_heat:.1f}°F"})
            if eco_cool is not None:
                metrics.append({"label": "Eco cool", "value": f"{eco_cool:.1f}°F"})
        if humidity is not None:
            metrics.append({"label": "Humidity", "value": f"{humidity}%"})

        # History: normalize current temp to 0–100 within 40–95 °F window
        hist_val: float = 50.0
        if temp_f is not None:
            hist_val = max(0.0, min(100.0, (temp_f - _TEMP_MIN_F) / (_TEMP_MAX_F - _TEMP_MIN_F) * 100.0))

        return {
            "available": True,
            "error":     None,
            "display": {
                "primary":  {"value": primary_value, "unit": dev_name, "color": primary_color},
                "badges":   badges,
                "metrics":  metrics,
                "detail":   self._detail_line(reading),
            },
            "history":       [hist_val],
            "history_label": "Temperature (°F)",
            "actions":       self.get_actions(avail),
        }

    def _detail_line(self, r: dict) -> str:
        mode = r.get("mode", "UNKNOWN")
        hvac = r.get("hvac_status", "OFF")
        temp_f = r.get("temp_f")
        parts = []
        if temp_f is not None:
            parts.append(f"{temp_f:.1f}°F ambient")
        if hvac == "HEATING":
            parts.append("currently heating")
        elif hvac == "COOLING":
            parts.append("currently cooling")
        if mode not in ("UNKNOWN",):
            parts.append(f"mode: {mode.replace('_', ' ').lower()}")
        return ", ".join(parts) if parts else ""

    def get_actions(self, available_modes: list | None = None) -> list[dict]:
        avail = set(available_modes or ["HEAT", "COOL", "HEATCOOL", "OFF"])
        actions = [
            {
                "id":             "set_heat",
                "label":          "Set Heat",
                "icon":           "🔆",
                "color":          "#f44336",
                "requires_input": True,
                "input_prompt":   "Heat to (°F):",
                "input_param":    "temperature",
            },
            {
                "id":             "set_cool",
                "label":          "Set Cool",
                "icon":           "🧊",
                "color":          "#2196f3",
                "requires_input": True,
                "input_prompt":   "Cool to (°F):",
                "input_param":    "temperature",
            },
        ]
        if "HEAT" in avail:
            actions.append({"id": "mode_heat", "label": "Heat", "icon": "🔆", "color": "#f44336"})
        if "COOL" in avail:
            actions.append({"id": "mode_cool", "label": "Cool", "icon": "🧊", "color": "#2196f3"})
        if "HEATCOOL" in avail:
            actions.append({"id": "mode_range", "label": "Heat+Cool", "icon": "↕️", "color": "#9c27b0"})
        actions.append({"id": "mode_eco", "label": "Eco", "icon": "🌿", "color": "#4caf50"})
        actions.append({"id": "mode_off",  "label": "Off",  "icon": "⏹️",  "color": "#9e9e9e"})
        actions.append({
            "id":             "run_fan",
            "label":          "Run Fan",
            "icon":           "💨",
            "color":          "#00bcd4",
            "requires_input": True,
            "input_prompt":   "Run fan for how many minutes?",
            "input_param":    "minutes",
        })
        actions.append({"id": "auth", "label": "OAuth URL", "icon": "🔑", "color": "#ff9800"})
        actions.append({
            "id":             "exchange_code",
            "label":          "Exchange Auth Code",
            "icon":           "🔐",
            "color":          "#9c27b0",
            "requires_input": True,
            "input_prompt":   "Paste Google auth code:",
            "input_param":    "code",
        })
        return actions

    def execute_action(self, action: str, params: dict | None = None) -> dict:
        p = params or {}

        if action == "auth":
            if self._svc is None:
                return {"ok": False, "message": "Service not started"}
            url = self._svc.build_auth_url()
            return {"ok": True, "message": url, "auth_url": url}

        if action == "exchange_code":
            if self._svc is None:
                return {"ok": False, "message": "Service not started"}
            ok, out = self._svc.exchange_auth_code(str(p.get("code", "")))
            if not ok:
                detail = out.get("detail")
                msg = out.get("error", "Code exchange failed")
                if detail:
                    msg = f"{msg}: {detail}"
                return {"ok": False, "message": msg}
            token = out.get("refresh_token", "")
            return {
                "ok": True,
                "message": (
                    "Received refresh token. Run "
                    f"`vera iot config nest_thermostat refresh_token={token}` "
                    "to save it."
                ),
                "refresh_token": token,
            }

        if self._svc is None or self._svc.degraded:
            reason = getattr(self._svc, "_degraded_reason", "Service unavailable") if self._svc else "Service not started"
            return {"ok": False, "message": reason}

        if action == "set_heat":
            try:
                temp_f = float(p.get("temperature", 0))
            except (ValueError, TypeError):
                return {"ok": False, "message": "Invalid temperature"}
            ok, msg = self._svc.set_heat(temp_f)
            return {"ok": ok, "message": msg if not ok else f"Heat set to {temp_f:.0f}°F"}

        if action == "set_cool":
            try:
                temp_f = float(p.get("temperature", 0))
            except (ValueError, TypeError):
                return {"ok": False, "message": "Invalid temperature"}
            ok, msg = self._svc.set_cool(temp_f)
            return {"ok": ok, "message": msg if not ok else f"Cool set to {temp_f:.0f}°F"}

        if action == "run_fan":
            try:
                minutes = int(float(p.get("minutes", 30)))
            except (ValueError, TypeError):
                return {"ok": False, "message": "Invalid minutes"}
            ok, msg = self._svc.run_fan(minutes)
            return {"ok": ok, "message": msg if not ok else f"Fan running for {minutes} min"}

        _MODE_MAP = {
            "mode_heat":  "HEAT",
            "mode_cool":  "COOL",
            "mode_range": "HEATCOOL",
            "mode_eco":   "ECO",
            "mode_off":   "OFF",
        }
        if action in _MODE_MAP:
            mode = _MODE_MAP[action]
            ok, msg = self._svc.set_mode(mode)
            label = mode.replace("_", " ").title()
            return {"ok": ok, "message": msg if not ok else f"Mode set to {label}"}

        return {"ok": False, "message": f"Unknown action '{action}'"}

    def announce(self) -> str:
        if self._svc is None or self._svc.degraded:
            return "Nest thermostat is not available."
        reading = self._svc.get_reading()
        if reading is None:
            return "Nest thermostat data is not yet available."

        temp_f   = reading.get("temp_f")
        hvac     = reading.get("hvac_status", "OFF")
        mode     = reading.get("mode", "UNKNOWN")
        heat_f   = reading.get("heat_f")
        cool_f   = reading.get("cool_f")
        humidity = reading.get("humidity")

        parts = []
        if temp_f is not None:
            parts.append(f"The thermostat reads {temp_f:.0f} degrees")
        if hvac == "HEATING":
            parts.append("currently heating")
        elif hvac == "COOLING":
            parts.append("currently cooling")
        if mode == "HEAT" and heat_f is not None:
            parts.append(f"set to heat to {heat_f:.0f} degrees")
        elif mode == "COOL" and cool_f is not None:
            parts.append(f"set to cool to {cool_f:.0f} degrees")
        elif mode == "HEATCOOL":
            if heat_f and cool_f:
                parts.append(f"range {heat_f:.0f} to {cool_f:.0f} degrees")
        elif mode == "MANUAL_ECO":
            parts.append("eco mode active")
        if humidity is not None:
            parts.append(f"humidity at {humidity} percent")

        text = (", ".join(parts) + ".") if parts else "Thermostat status unavailable."
        if self.bus:
            self.bus.publish("av.say", {"text": text})
        return text
