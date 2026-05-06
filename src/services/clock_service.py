"""
Clock announcement service.

Wraps ClockAnnouncer as a Service so it participates in the standard
start/stop lifecycle and receives the shared MessageBus.

Topics published (via bus, not directly):
    av.say    {"text": str}   — time announcement ± dad joke

Config (config/assistant.yaml):
    clock_announcements:
      enabled: true      # set false to silence entirely at startup
"""

from __future__ import annotations

import logging
from typing import Optional

from src.audio.clock_announcer import ClockAnnouncer
from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class ClockService(Service):
    name = "clock"
    tick_seconds = 0   # no polling needed — ClockAnnouncer runs its own thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        enabled: bool = True,
    ) -> None:
        super().__init__(bus=bus)
        self._enabled = enabled
        self._announcer: Optional[ClockAnnouncer] = None

    # ------------------------------------------------------------------
    # Property to toggle at runtime (e.g. from IPC command)
    # ------------------------------------------------------------------

    @property
    def clock_enabled(self) -> bool:
        return self._announcer.enabled if self._announcer else self._enabled

    @clock_enabled.setter
    def clock_enabled(self, value: bool) -> None:
        self._enabled = value
        if self._announcer:
            self._announcer.enabled = value
            log.info("ClockService: clock announcements %s",
                     "enabled" if value else "disabled")

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        def _say(text: str) -> None:
            self.bus.publish("av.say", {"text": text})

        self._announcer = ClockAnnouncer(say_fn=_say, enabled=self._enabled)
        self._announcer.start()
        log.info("ClockService started (enabled=%s)", self._enabled)

    def run_tick(self) -> None:
        pass  # ClockAnnouncer drives itself

    def on_stop(self) -> None:
        if self._announcer is not None:
            self._announcer.stop()
        log.info("ClockService stopped")
