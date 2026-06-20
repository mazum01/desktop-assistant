"""IoTHistoryStore — persistent, time-aware per-device sparkline history.

Maintains timestamped history points for each IoT device and persists them to
disk. Each device can use its own retention horizon and sampling interval.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_PATH = Path.home() / ".local/share/desktop-assistant/iot_history.json"
MAX_POINTS = 5000
_DEFAULT_HORIZON_S = 2 * 60 * 60
_DEFAULT_SAMPLE_S = 60.0
_MIN_SAMPLE_S = 1.0


class IoTHistoryStore:
    """Thread-safe ring-buffer store for IoT sparkline history."""

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = path or _DEFAULT_PATH
        self._lock = threading.Lock()
        self._data: dict[str, list[tuple[float, float]]] = {}
        self._dirty = False
        self._load()

    # ── Public API ────────────────────────────────────────────────────────────

    def push(
        self,
        device_id: str,
        values: list[float],
        *,
        horizon_s: int = _DEFAULT_HORIZON_S,
        sample_interval_s: float = _DEFAULT_SAMPLE_S,
        now_ts: float | None = None,
    ) -> None:
        """Append values to device buffer using per-device horizon/frequency.

        If the buffer is currently empty *and* more than one value is given
        (i.e. a device seeding its full internal history on first start), all
        values are stored rather than just the last one.
        """
        if not values:
            return
        now = float(now_ts) if now_ts is not None else float(time.time())
        horizon = self._coerce_horizon_s(horizon_s)
        sample_s = self._coerce_sample_s(sample_interval_s)
        with self._lock:
            buf = self._data.setdefault(device_id, [])
            if not buf and len(values) > 1:
                seed = values[-MAX_POINTS:]
                start = now - sample_s * max(len(seed) - 1, 0)
                for i, val in enumerate(seed):
                    buf.append((start + i * sample_s, float(val)))
            else:
                latest = float(values[-1])
                if not buf:
                    buf.append((now, latest))
                else:
                    last_ts, _last_val = buf[-1]
                    if now - last_ts >= sample_s:
                        buf.append((now, latest))
                    else:
                        # Keep newest value without increasing sample density.
                        buf[-1] = (last_ts, latest)
                self._trim_locked(buf, now=now, horizon_s=horizon)
            self._dirty = True

    def get(
        self,
        device_id: str,
        *,
        horizon_s: int | None = None,
        now_ts: float | None = None,
    ) -> list[float]:
        """Return a copy of values for a device, trimmed to requested horizon."""
        now = float(now_ts) if now_ts is not None else float(time.time())
        horizon = self._coerce_horizon_s(horizon_s) if horizon_s is not None else None
        with self._lock:
            buf = self._data.get(device_id, [])
            if not buf:
                return []
            if horizon is not None:
                before = len(buf)
                self._trim_locked(buf, now=now, horizon_s=horizon)
                if len(buf) != before:
                    self._dirty = True
            return [v for _ts, v in buf]

    def save(self) -> None:
        """Persist the store to disk (no-op if nothing changed)."""
        with self._lock:
            if not self._dirty:
                return
            data_copy: dict[str, Any] = {
                dev: [[round(ts, 3), val] for ts, val in points]
                for dev, points in self._data.items()
            }
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
                now = time.time()
                for k, v in raw.items():
                    if not isinstance(v, list):
                        continue
                    if v and isinstance(v[0], list) and len(v[0]) == 2:
                        pts: list[tuple[float, float]] = []
                        for item in v[-MAX_POINTS:]:
                            try:
                                ts = float(item[0])
                                val = float(item[1])
                            except Exception:
                                continue
                            pts.append((ts, val))
                        self._data[k] = pts
                    else:
                        # Backward compatibility: old format was a simple value list.
                        vals = []
                        for item in v[-MAX_POINTS:]:
                            try:
                                vals.append(float(item))
                            except Exception:
                                continue
                        start = now - _DEFAULT_SAMPLE_S * max(len(vals) - 1, 0)
                        self._data[k] = [
                            (start + i * _DEFAULT_SAMPLE_S, val)
                            for i, val in enumerate(vals)
                        ]
            log.info("IoTHistoryStore: loaded history for %d device(s) from %s",
                     len(self._data), self._path)
        except Exception:
            log.exception("IoTHistoryStore: failed to load %s — starting fresh", self._path)

    @staticmethod
    def _coerce_horizon_s(raw: int | None) -> int:
        try:
            horizon = int(raw) if raw is not None else _DEFAULT_HORIZON_S
        except Exception:
            horizon = _DEFAULT_HORIZON_S
        return max(60, horizon)

    @staticmethod
    def _coerce_sample_s(raw: float | None) -> float:
        try:
            sample = float(raw) if raw is not None else _DEFAULT_SAMPLE_S
        except Exception:
            sample = _DEFAULT_SAMPLE_S
        return max(_MIN_SAMPLE_S, sample)

    @staticmethod
    def _trim_locked(buf: list[tuple[float, float]], *, now: float, horizon_s: int) -> None:
        if not buf:
            return
        cutoff = now - float(horizon_s)
        idx = 0
        for ts, _v in buf:
            if ts >= cutoff:
                break
            idx += 1
        if idx > 0:
            del buf[:idx]
        if len(buf) > MAX_POINTS:
            del buf[: len(buf) - MAX_POINTS]
