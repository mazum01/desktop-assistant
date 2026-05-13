"""NotificationService — proactive speech and system alerts.

Monitors system health and presence, and speaks proactively when:

1. **Thermal alert** — CPU temperature crosses a warning or critical threshold.
   Rate-limited per severity so the same alert isn't repeated too often.

2. **Long absence** — no face has been seen for ``absence_alert_min`` minutes.
   DA says something to draw attention when a person wanders back into range
   (or just hasn't been around in a while).

Topics subscribed
-----------------
thermal.temp    ``{"celsius": float, "fan_duty": float}``
perception.faces ``{count: int, …}``
av.spoke        (suppresses alerts while TTS is active)

Topics published
----------------
av.say          ``{"text": str}``
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.core.service import Service

log = logging.getLogger(__name__)

# How often the background thread wakes to check absence (seconds)
_CHECK_INTERVAL_S = 30.0


class NotificationService(Service):
    """Proactive speech: thermal alerts and long-absence greetings."""

    name = "notification"

    def __init__(
        self,
        bus=None,
        quiet_hours=None,
        thermal_alerts_enabled: bool = True,
        warn_celsius: float = 75.0,
        critical_celsius: float = 85.0,
        thermal_rate_limit_min: float = 10.0,
        absence_alerts_enabled: bool = True,
        absence_min: float = 30.0,
        absence_rate_limit_min: float = 60.0,
    ) -> None:
        super().__init__(bus=bus)
        self._qh = quiet_hours
        self._thermal_alerts_enabled = thermal_alerts_enabled
        self._warn_celsius = warn_celsius
        self._critical_celsius = critical_celsius
        self._thermal_rate_limit_s = thermal_rate_limit_min * 60.0
        self._absence_alerts_enabled = absence_alerts_enabled
        self._absence_s = absence_min * 60.0
        self._absence_rate_limit_s = absence_rate_limit_min * 60.0

        self._last_temp: float = 0.0
        self._speaking: bool = False
        self._last_face_seen: float = time.monotonic()  # assume someone is here at start
        self._last_notified: dict[str, float] = {}  # notification_type → last fired ts

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._unsubs: list = []

    # ── Service lifecycle ─────────────────────────────────────────────

    def on_start(self) -> None:
        self._unsubs += [
            self.bus.subscribe("thermal.temp",     self._on_thermal),
            self.bus.subscribe("perception.faces", self._on_faces),
            self.bus.subscribe("av.spoke",         self._on_spoke),
            self.bus.subscribe("av.speaking_started", self._on_speaking_started),
        ]
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop,
            name="notification-monitor",
            daemon=True,
        )
        self._thread.start()
        log.info(
            "NotificationService started (thermal=%s warn=%.0f°C crit=%.0f°C, "
            "absence=%s absence_min=%.0f)",
            self._thermal_alerts_enabled, self._warn_celsius, self._critical_celsius,
            self._absence_alerts_enabled, self._absence_s / 60.0,
        )

    def on_stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        log.info("NotificationService stopped")

    # ── Bus handlers ─────────────────────────────────────────────────

    def _on_thermal(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        temp = payload.get("celsius")
        if temp is None:
            return
        self._last_temp = float(temp)

        if not self._thermal_alerts_enabled:
            return

        if self._last_temp >= self._critical_celsius:
            self._maybe_notify(
                "thermal_critical",
                f"Warning! My CPU temperature is {self._last_temp:.0f} degrees Celsius — "
                "running very hot! Consider reducing load.",
            )
        elif self._last_temp >= self._warn_celsius:
            self._maybe_notify(
                "thermal_warn",
                f"Heads up — my CPU is {self._last_temp:.0f} degrees Celsius. "
                "I'm running a bit warm.",
            )

    def _on_faces(self, _topic, payload) -> None:
        if isinstance(payload, dict) and payload.get("count", 0) > 0:
            self._last_face_seen = time.monotonic()

    def _on_speaking_started(self, _topic, _payload) -> None:
        self._speaking = True

    def _on_spoke(self, _topic, _payload) -> None:
        self._speaking = False

    # ── Background monitor ────────────────────────────────────────────

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(_CHECK_INTERVAL_S):
            self._check_absence()

    def _check_absence(self) -> None:
        if not self._absence_alerts_enabled:
            return
        absence_s = time.monotonic() - self._last_face_seen
        if absence_s >= self._absence_s:
            self._maybe_notify(
                "absence",
                "I haven't seen anyone in a while. Let me know if you need anything!",
            )

    # ── Helpers ───────────────────────────────────────────────────────

    def _maybe_notify(self, notification_type: str, text: str) -> None:
        """Speak *text* unless rate-limited, speaking is active, or quiet hours."""
        if self._speaking:
            return
        if self._qh is not None and self._qh.is_quiet():
            return

        rate_limit = (
            self._absence_rate_limit_s
            if notification_type == "absence"
            else self._thermal_rate_limit_s
        )
        now = time.monotonic()
        if now - self._last_notified.get(notification_type, 0.0) < rate_limit:
            return

        self._last_notified[notification_type] = now
        log.info("NotificationService: speaking %r (%s)", text[:60], notification_type)
        self.bus.publish("av.say", {"text": text})
