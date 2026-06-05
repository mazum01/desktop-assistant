"""IoTHistoryStore — persistent per-device sparkline history.

Maintains a ring buffer (up to MAX_POINTS readings) for every IoT device
and persists it to a JSON file so history survives daemon restarts and
browser refreshes.

The store is injected into IoTRegistry, which calls push() after every
get_all_snapshots() call.  Individual device plugins do not need to change.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".local/share/desktop-assistant/iot_history.json"
MAX_POINTS = 120   # ~2 h at 60 s Nest polling; ~10 h at radon 5-min interval


class IoTHistoryStore:
    """Thread-safe ring-buffer store for IoT sparkline history."""

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or _DEFAULT_PATH
        self._lock = threading.Lock()
        self._data: dict[str, list[float]] = {}
        self._dirty = False
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def push(self, device_id: str, values: list[float]) -> None:
        """Append *values* to *device_id*'s buffer; trims to MAX_POINTS.

        If the buffer is currently empty *and* more than one value is given
        (i.e. a device seeding its full internal history on first start), all
        values are stored rather than just the last one.
        """
        if not values:
            return
        with self._lock:
            buf = self._data.setdefault(device_id, [])
            if not buf and len(values) > 1:
                # Seed from full deque (radon / drop first start)
                buf.extend(values[-MAX_POINTS:])
            else:
                # Append only the latest point each poll cycle
                buf.append(values[-1])
                if len(buf) > MAX_POINTS:
                    del buf[:len(buf) - MAX_POINTS]
            self._dirty = True

    def get(self, device_id: str) -> list[float]:
        """Return a copy of the history buffer for *device_id*."""
        with self._lock:
            return list(self._data.get(device_id, []))

    def save(self) -> None:
        """Persist the store to disk (no-op if nothing changed)."""
        with self._lock:
            if not self._dirty:
                return
            data_copy: dict[str, Any] = dict(self._data)
            self._dirty = False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data_copy, separators=(",", ":")))
            os.replace(tmp, self._path)
            log.debug("IoTHistoryStore: saved %d devices to %s", len(data_copy), self._path)
        except Exception:
            log.exception("IoTHistoryStore: save failed")

    # ── Internal ──────────────────────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
            if isinstance(raw, dict):
                for k, v in raw.items():
                    if isinstance(v, list):
                        self._data[k] = [float(x) for x in v[-MAX_POINTS:]]
            log.info("IoTHistoryStore: loaded history for %d device(s) from %s",
                     len(self._data), self._path)
        except Exception:
            log.exception("IoTHistoryStore: failed to load %s — starting fresh", self._path)
