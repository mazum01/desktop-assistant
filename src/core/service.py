"""
Service base class.

Standardises the lifecycle for every long-running service in the
assistant: thermal, motion, AV, perception, dialog, etc.

Lifecycle:
    on_start()   — open hardware, subscribe to bus topics
    run_tick()   — called repeatedly at ~1 Hz (override for periodic work)
    on_stop()    — release hardware, unsubscribe

Each service owns one daemon thread. Services are coordinated via the
shared `MessageBus`, not direct method calls — so they can be started,
stopped, and replaced independently.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from src.core.bus import MessageBus, default_bus

log = logging.getLogger(__name__)


class Service:
    """Base class — subclass and override `on_start`, `run_tick`, `on_stop`."""

    name: str = "service"
    tick_seconds: float = 1.0  # how often run_tick() is called

    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        self.bus: MessageBus = bus or default_bus()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._running = False

    # ── Override these in subclasses ───────────────────────────────────

    def on_start(self) -> None:
        """Open hardware, register bus subscriptions. Default: no-op."""

    def run_tick(self) -> None:
        """Called every tick_seconds while running. Default: no-op."""

    def on_stop(self) -> None:
        """Release hardware, unregister subscriptions. Default: no-op."""

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            log.warning("%s already running", self.name)
            return
        log.info("Starting service: %s", self.name)
        self._stop_event.clear()
        self.on_start()
        self.bus.publish("service.started", {"name": self.name})

        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"svc-{self.name}",
            daemon=True,
        )
        self._running = True
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._running:
            return
        log.info("Stopping service: %s", self.name)
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning("%s did not stop within %.1fs", self.name, timeout)
        self._running = False
        try:
            self.on_stop()
        except Exception:
            log.exception("on_stop() raised in %s", self.name)
        self.bus.publish("service.stopped", {"name": self.name})

    def is_running(self) -> bool:
        return self._running

    # ── Internal ───────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            tick_start = time.monotonic()
            try:
                self.run_tick()
            except Exception:
                log.exception("run_tick() error in %s", self.name)
            elapsed = time.monotonic() - tick_start
            sleep_for = max(0.0, self.tick_seconds - elapsed)
            if self._stop_event.wait(sleep_for):
                break

    # ── Context manager ────────────────────────────────────────────────

    def __enter__(self) -> "Service":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()
