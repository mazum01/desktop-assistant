"""
In-process message bus.

A lightweight pub/sub used by the assistant's services to communicate
without coupling. Designed for simplicity over performance: the whole
assistant runs in one Python process, so we don't need ZeroMQ or
similar — just thread-safe topic dispatch.

Topics are dotted strings (e.g. "thermal.temp", "motion.target",
"perception.face_detected"). Subscribers register a callback per topic;
publishers call `publish(topic, payload)`.

Callbacks run **synchronously** in the publisher's thread by default.
For background work, the subscriber should hand off to its own queue.
This keeps the bus predictable and lock-free for fast handlers.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, DefaultDict, List

log = logging.getLogger(__name__)

Callback = Callable[[str, Any], None]


@dataclass
class _Subscription:
    callback: Callback
    once: bool = False


class MessageBus:
    """Thread-safe in-process pub/sub with per-topic subscriber lists."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: DefaultDict[str, List[_Subscription]] = defaultdict(list)
        self._wildcards: List[_Subscription] = []
        self._last_payload: dict[str, Any] = {}

    # ── Subscribe / unsubscribe ─────────────────────────────────────────

    def subscribe(self, topic: str, callback: Callback) -> Callable[[], None]:
        """
        Register *callback* for *topic* (or "*" for all topics).
        Returns an unsubscribe function.
        """
        sub = _Subscription(callback=callback)
        with self._lock:
            if topic == "*":
                self._wildcards.append(sub)
            else:
                self._subs[topic].append(sub)

        def _unsub() -> None:
            with self._lock:
                if topic == "*":
                    if sub in self._wildcards:
                        self._wildcards.remove(sub)
                else:
                    if sub in self._subs[topic]:
                        self._subs[topic].remove(sub)

        return _unsub

    def subscribe_once(self, topic: str, callback: Callback) -> None:
        """Register a one-shot subscription for *topic*."""
        sub = _Subscription(callback=callback, once=True)
        with self._lock:
            if topic == "*":
                self._wildcards.append(sub)
            else:
                self._subs[topic].append(sub)

    # ── Publish ─────────────────────────────────────────────────────────

    def publish(self, topic: str, payload: Any = None) -> None:
        """Publish *payload* on *topic* synchronously to all subscribers."""
        with self._lock:
            self._last_payload[topic] = payload
            targets = list(self._subs.get(topic, [])) + list(self._wildcards)
            # Remove one-shot subscriptions before invoking
            for sub in targets:
                if sub.once:
                    self._remove_subscription(topic, sub)

        for sub in targets:
            try:
                sub.callback(topic, payload)
            except Exception:
                log.exception("Subscriber error on topic=%s", topic)

    def _remove_subscription(self, topic: str, sub: _Subscription) -> None:
        if topic == "*":
            if sub in self._wildcards:
                self._wildcards.remove(sub)
        else:
            if sub in self._subs.get(topic, []):
                self._subs[topic].remove(sub)

    # ── Introspection ───────────────────────────────────────────────────

    def last(self, topic: str, default: Any = None) -> Any:
        """Most recent payload published on *topic*, or *default*."""
        with self._lock:
            return self._last_payload.get(topic, default)

    def topics(self) -> list[str]:
        with self._lock:
            return sorted(self._subs.keys())

    def subscriber_count(self, topic: str) -> int:
        with self._lock:
            return len(self._subs.get(topic, [])) + len(self._wildcards)

    def clear(self) -> None:
        """Reset the bus — primarily for tests."""
        with self._lock:
            self._subs.clear()
            self._wildcards.clear()
            self._last_payload.clear()


# ── Singleton accessor ──────────────────────────────────────────────────
# Services share one bus by default; tests can construct their own.

_default_bus: MessageBus | None = None
_default_bus_lock = threading.Lock()


def default_bus() -> MessageBus:
    """Return the process-wide default MessageBus, creating it on first call."""
    global _default_bus
    with _default_bus_lock:
        if _default_bus is None:
            _default_bus = MessageBus()
        return _default_bus
