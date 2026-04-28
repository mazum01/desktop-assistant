"""
Telemetry service — persists selected bus topics to SQLite for graphing
and post-mortem analysis. Backed by a fixed-size ring (oldest rows
deleted past a row cap) so it can never fill the disk.

Default topics recorded:
    thermal.temp     {"celsius", "fahrenheit", "ok"}
    thermal.fan      {"duty", "backend"}
    thermal.rpm      {"rpm"}
    motion.position  {"angle"}
    audio.level      {"dbfs", "rms"}

Schema:
    samples(ts REAL, topic TEXT, payload TEXT)
    INDEX ix_samples_topic_ts ON samples(topic, ts)

Topics published:
    telemetry.flush  {"rows": int}            — emitted after each flush
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sqlite3
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

_DEFAULT_TOPICS = (
    "thermal.temp",
    "thermal.fan",
    "thermal.rpm",
    "motion.position",
    "audio.level",
)

_DEFAULT_DB_PATH = (
    Path(os.environ.get("DA_TELEMETRY_DIR")
         or os.path.expanduser("~/.local/share/desktop-assistant"))
    / "telemetry.db"
)

# Each topic is independently capped — keeps a chatty topic from evicting
# everything else. 1 sample/sec × 7 days ≈ 605 k rows; 200 k is comfortably
# under that for the topics we record.
_DEFAULT_ROW_CAP_PER_TOPIC = 200_000
_FLUSH_INTERVAL_S          = 5.0
_QUEUE_LIMIT               = 10_000


class TelemetryService(Service):
    name = "telemetry"
    tick_seconds = _FLUSH_INTERVAL_S

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        db_path: Optional[Path] = None,
        topics: Iterable[str] = _DEFAULT_TOPICS,
        row_cap_per_topic: int = _DEFAULT_ROW_CAP_PER_TOPIC,
    ) -> None:
        super().__init__(bus=bus)
        self._db_path = Path(db_path) if db_path else _DEFAULT_DB_PATH
        self._topics  = tuple(topics)
        self._row_cap = int(row_cap_per_topic)
        self._queue: "queue.Queue[Tuple[float, str, str]]" = queue.Queue(_QUEUE_LIMIT)
        self._unsubs: List = []
        self._conn: Optional[sqlite3.Connection] = None
        self._writer_lock = threading.Lock()

    # ──────────────────────────────────────────────────────────────────
    # Lifecycle
    # ──────────────────────────────────────────────────────────────────

    def on_start(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path),
            check_same_thread=False,
            isolation_level=None,    # autocommit; we batch with BEGIN/COMMIT
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS samples ("
            "  ts REAL NOT NULL,"
            "  topic TEXT NOT NULL,"
            "  payload TEXT NOT NULL"
            ")"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_samples_topic_ts "
            "ON samples(topic, ts)"
        )

        for topic in self._topics:
            self._unsubs.append(self.bus.subscribe(topic, self._on_event))

        log.info(
            "TelemetryService recording %d topics → %s (cap %d rows/topic)",
            len(self._topics), self._db_path, self._row_cap,
        )

    def run_tick(self) -> None:
        self._flush()

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self._flush()
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        log.info("TelemetryService stopped")

    # ──────────────────────────────────────────────────────────────────
    # Public helpers (used by status command + tests)
    # ──────────────────────────────────────────────────────────────────

    def row_count(self, topic: Optional[str] = None) -> int:
        if self._conn is None:
            return 0
        if topic is None:
            cur = self._conn.execute("SELECT COUNT(*) FROM samples")
        else:
            cur = self._conn.execute(
                "SELECT COUNT(*) FROM samples WHERE topic=?", (topic,)
            )
        return int(cur.fetchone()[0])

    def recent(self, topic: str, limit: int = 100):
        if self._conn is None:
            return []
        cur = self._conn.execute(
            "SELECT ts, payload FROM samples WHERE topic=? "
            "ORDER BY ts DESC LIMIT ?",
            (topic, int(limit)),
        )
        return [(ts, json.loads(p)) for ts, p in cur.fetchall()]

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ──────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────

    def _on_event(self, topic: str, payload) -> None:
        try:
            blob = json.dumps(payload, default=str)
        except Exception:
            blob = json.dumps({"_repr": repr(payload)})
        try:
            self._queue.put_nowait((time.time(), topic, blob))
        except queue.Full:
            log.debug("telemetry queue full — dropping %s", topic)

    def _flush(self) -> None:
        if self._conn is None:
            return
        rows: List[Tuple[float, str, str]] = []
        while True:
            try:
                rows.append(self._queue.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return

        try:
            with self._writer_lock:
                self._conn.execute("BEGIN")
                self._conn.executemany(
                    "INSERT INTO samples(ts, topic, payload) VALUES (?, ?, ?)",
                    rows,
                )
                self._conn.execute("COMMIT")
                self._enforce_caps(set(t for _, t, _ in rows))
        except Exception:
            log.exception("Telemetry flush failed")
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass
            return

        self.bus.publish("telemetry.flush", {"rows": len(rows)})

    def _enforce_caps(self, touched_topics) -> None:
        for topic in touched_topics:
            self._conn.execute(
                "DELETE FROM samples WHERE topic=? AND ts < "
                "(SELECT ts FROM samples WHERE topic=? "
                "ORDER BY ts DESC LIMIT 1 OFFSET ?)",
                (topic, topic, self._row_cap),
            )
