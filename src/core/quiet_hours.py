"""
Quiet-hours gate.

Reads from ``config/quiet_hours.json`` (runtime-writable).
Falls back to assistant.yaml ``quiet_hours`` section, then to a
hard default of disabled.

Usage::

    qh = QuietHours.from_config(cfg_dir)
    if qh.is_quiet():
        return  # skip noisy action
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

_FILENAME = "quiet_hours.json"


class QuietHours:
    """Thread-safe quiet-hours gate; config can be updated at runtime."""

    def __init__(
        self,
        enabled: bool = False,
        start: str = "21:00",
        end: str = "06:00",
        config_path: Optional[Path] = None,
    ) -> None:
        self._enabled = enabled
        self._start = start
        self._end = end
        self._path = config_path

    # ── Factory ───────────────────────────────────────────────────────

    @classmethod
    def from_config(cls, cfg_dir: Path, yaml_defaults: dict | None = None) -> "QuietHours":
        """Load from ``cfg_dir/quiet_hours.json``; seed from yaml_defaults if missing."""
        path = cfg_dir / _FILENAME
        defaults = (yaml_defaults or {})
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception as exc:
                log.warning("Failed to load quiet_hours.json (%s), using defaults", exc)
                data = {}
        else:
            data = {}

        obj = cls(
            enabled=data.get("enabled", defaults.get("enabled", False)),
            start=data.get("start", defaults.get("start", "21:00")),
            end=data.get("end", defaults.get("end", "06:00")),
            config_path=path,
        )
        # Persist defaults if the file didn't exist yet
        if not path.exists():
            try:
                obj._write()
            except Exception:
                pass
        return obj

    # ── Properties ────────────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def start(self) -> str:
        return self._start

    @property
    def end(self) -> str:
        return self._end

    # ── Gate ──────────────────────────────────────────────────────────

    def is_quiet(self, dt: Optional[datetime] = None) -> bool:
        """Return True if the given datetime (default: now) falls in quiet hours."""
        if not self._enabled:
            return False
        now: time = (dt or datetime.now()).time()
        try:
            start = time.fromisoformat(self._start)
            end = time.fromisoformat(self._end)
        except ValueError:
            log.warning("QuietHours: invalid time format (start=%r, end=%r)", self._start, self._end)
            return False

        if start <= end:
            # Same-day range, e.g. 08:00–18:00
            return start <= now < end
        else:
            # Overnight range, e.g. 21:00–06:00
            return now >= start or now < end

    # ── Update ────────────────────────────────────────────────────────

    def update(self, enabled: bool, start: str, end: str) -> None:
        """Update settings and persist to disk."""
        # Validate times before saving
        time.fromisoformat(start)
        time.fromisoformat(end)
        self._enabled = enabled
        self._start = start
        self._end = end
        self._write()
        log.info("QuietHours updated: enabled=%s, %s–%s", enabled, start, end)

    def as_dict(self) -> dict:
        return {"enabled": self._enabled, "start": self._start, "end": self._end}

    # ── Internal ──────────────────────────────────────────────────────

    def _write(self) -> None:
        if self._path is None:
            return
        self._path.write_text(json.dumps(self.as_dict(), indent=2) + "\n")
