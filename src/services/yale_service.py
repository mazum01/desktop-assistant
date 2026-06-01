"""Yale Smart Lock service — polls lock state via yalesmartalarmclient.

The service runs a background polling thread that refreshes lock states every
``poll_interval`` seconds.  Lock/unlock operations are synchronous HTTP calls
executed on the calling thread.

Config keys (passed via ``cfg`` dict):
    username        Yale / August app e-mail address.
    password        Yale / August app password.
    lock_name       Name of the lock to target (optional; first lock if omitted).
    poll_interval   Polling interval in seconds (default: 30).

The service is ``degraded`` when:
  - ``yalesmartalarmclient`` is not installed, OR
  - authentication fails, OR
  - ``username`` / ``password`` are not configured.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

_POLL_DEFAULT = 30  # seconds between lock-state refreshes


class YaleService:
    """Background-polling adapter for the Yale Smart Alarm / August cloud API."""

    def __init__(self, bus: Any = None, cfg: dict | None = None) -> None:
        self.bus = bus
        self._cfg = cfg or {}
        self.degraded = False
        self._degraded_reason = ""

        self._client: Any = None
        self._lock_obj: Any = None
        self._reading: dict | None = None
        self._reading_lock = threading.Lock()

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Validate dependencies
        try:
            import yalesmartalarmclient  # noqa: F401
        except ImportError:
            self.degraded = True
            self._degraded_reason = (
                "yalesmartalarmclient not installed — "
                "run: sudo pip install --break-system-packages yalesmartalarmclient"
            )
            log.warning("YaleService: %s", self._degraded_reason)
            return

        if not self._cfg.get("username") or not self._cfg.get("password"):
            self.degraded = True
            self._degraded_reason = (
                "Yale username/password not configured. "
                "Add via 'vera iot config yale_lock username=EMAIL password=PASS'"
            )
            log.warning("YaleService: %s", self._degraded_reason)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Connect to Yale cloud and start background polling."""
        if self.degraded:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._poll_loop,
            name="yale-poll",
            daemon=True,
        )
        self._thread.start()
        log.info("YaleService: started polling thread")

    def stop(self) -> None:
        """Stop the background polling thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None
        log.info("YaleService: stopped")

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll_loop(self) -> None:
        interval = float(self._cfg.get("poll_interval", _POLL_DEFAULT))
        while not self._stop_event.is_set():
            try:
                self._refresh()
            except Exception:
                log.exception("YaleService: poll error")
            self._stop_event.wait(interval)

    def _get_client(self) -> Any:
        """Return cached client, creating it if needed."""
        if self._client is None:
            from yalesmartalarmclient.client import YaleSmartAlarmClient
            self._client = YaleSmartAlarmClient(
                self._cfg["username"],
                self._cfg["password"],
            )
            log.info("YaleService: connected to Yale cloud")
        return self._client

    def _get_target_lock(self, client: Any) -> Any:
        """Return the target YaleLock object."""
        lock_name = self._cfg.get("lock_name", "").strip()
        if lock_name:
            lock = client.lock_api.get(name=lock_name)
            if lock is None:
                names = [l.name for l in client.lock_api.locks]
                raise ValueError(
                    f"Lock {lock_name!r} not found. Available: {names}"
                )
            return lock
        # Default: first available lock
        locks = client.lock_api.locks
        if not locks:
            raise ValueError("No Yale locks found on this account")
        return locks[0]

    def _refresh(self) -> None:
        """Fetch fresh lock state from Yale cloud and cache it."""
        from yalesmartalarmclient.lock import YaleLockState
        from yalesmartalarmclient.exceptions import AuthenticationError

        try:
            client = self._get_client()
            # Force a refresh of lock list
            client.lock_api.locks = list(client.lock_api.get_locks())
            lock = self._get_target_lock(client)
            state = lock.state()

            state_str = {
                YaleLockState.LOCKED:    "locked",
                YaleLockState.UNLOCKED:  "unlocked",
                YaleLockState.DOOR_OPEN: "door_open",
                YaleLockState.UNKNOWN:   "unknown",
            }.get(state, "unknown")

            with self._reading_lock:
                self._reading = {
                    "name":       lock.name,
                    "state":      state_str,
                    "autolock":   lock.autolock(),
                }
            self.degraded = False
            self._degraded_reason = ""

        except AuthenticationError as exc:
            self.degraded = True
            self._degraded_reason = f"Yale authentication failed: {exc}"
            self._client = None  # force re-auth next cycle
            log.error("YaleService: %s", self._degraded_reason)

        except Exception as exc:
            log.warning("YaleService: refresh error: %s", exc)
            # Don't mark degraded on transient network errors; keep last reading

    # ── Public API ────────────────────────────────────────────────────────────

    def get_reading(self) -> dict | None:
        """Return the latest cached lock state, or ``None`` if not yet available."""
        with self._reading_lock:
            return dict(self._reading) if self._reading else None

    def lock(self) -> tuple[bool, str]:
        """Lock the door immediately.

        Returns ``(True, "")`` on success, ``(False, error_msg)`` on failure.
        """
        if self.degraded:
            return False, self._degraded_reason
        try:
            client = self._get_client()
            lock_obj = self._get_target_lock(client)
            ok = lock_obj.close()
            if ok:
                # Optimistically update cached state
                with self._reading_lock:
                    if self._reading:
                        self._reading["state"] = "locked"
            return bool(ok), "" if ok else "Lock command rejected by Yale API"
        except Exception as exc:
            return False, str(exc)

    def unlock(self, pin: str = "") -> tuple[bool, str]:
        """Unlock the door with the given PIN.

        Returns ``(True, "")`` on success, ``(False, error_msg)`` on failure.
        The PIN defaults to ``unlock_pin`` from config if not passed explicitly.
        """
        if self.degraded:
            return False, self._degraded_reason
        effective_pin = pin or self._cfg.get("unlock_pin", "")
        if not effective_pin:
            return False, "PIN required for unlock — set via iot config yale_lock unlock_pin=XXXX"
        try:
            client = self._get_client()
            lock_obj = self._get_target_lock(client)
            ok = lock_obj.open(pin_code=str(effective_pin))
            if ok:
                with self._reading_lock:
                    if self._reading:
                        self._reading["state"] = "unlocked"
            return bool(ok), "" if ok else "Unlock command rejected by Yale API"
        except Exception as exc:
            return False, str(exc)

    def get_all_locks(self) -> list[dict]:
        """Return a list of all lock names/states for this account."""
        if self.degraded:
            return []
        try:
            from yalesmartalarmclient.lock import YaleLockState
            client = self._get_client()
            result = []
            for lock in client.lock_api.locks:
                state = lock.state()
                state_str = {
                    YaleLockState.LOCKED:    "locked",
                    YaleLockState.UNLOCKED:  "unlocked",
                    YaleLockState.DOOR_OPEN: "door_open",
                    YaleLockState.UNKNOWN:   "unknown",
                }.get(state, "unknown")
                result.append({"name": lock.name, "state": state_str})
            return result
        except Exception as exc:
            log.warning("YaleService: get_all_locks error: %s", exc)
            return []
