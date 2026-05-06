"""
Face identity registry — persistent SQLite store for face embeddings and names.

Schema
------
faces
    id           TEXT PK    UUID
    name         TEXT       "Guest 1" until named
    first_seen   REAL       Unix timestamp
    last_seen    REAL       Unix timestamp
    last_greeted REAL       Unix timestamp (0 = never)
    seen_count   INTEGER

face_embeddings
    id           TEXT PK    UUID
    face_id      TEXT FK    references faces.id
    embedding    BLOB       512 × float32
    created_at   REAL

Usage::

    registry = FaceRegistry()
    face_id, name, score = registry.find_match(embedding) or registry.register(embedding)
"""

from __future__ import annotations

import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

log = logging.getLogger(__name__)

_DEFAULT_DB = Path.home() / ".local" / "share" / "desktop-assistant" / "faces.db"
_MATCH_THRESHOLD = 0.45      # cosine similarity ≥ this → same person
_EMBED_DIM = 512


class FaceRegistry:
    """Persistent face identity store backed by SQLite.

    Thread-safe for single-writer usage; all public methods acquire the same
    connection on the calling thread (``check_same_thread=False`` so the
    PerceptionService thread can use the same instance created on the main thread).
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        match_threshold: float = _MATCH_THRESHOLD,
    ) -> None:
        path = Path(db_path) if db_path else _DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._threshold = match_threshold
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        log.info("FaceRegistry opened: %s", path)

    # ── Schema ───────────────────────────────────────────────────────────

    def _ensure_schema(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS faces (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                first_seen   REAL NOT NULL,
                last_seen    REAL NOT NULL,
                last_greeted REAL NOT NULL DEFAULT 0,
                seen_count   INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS face_embeddings (
                id         TEXT PRIMARY KEY,
                face_id    TEXT NOT NULL REFERENCES faces(id),
                embedding  BLOB NOT NULL,
                created_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_emb_face ON face_embeddings(face_id);
        """)
        self._conn.commit()

    # ── Public API ───────────────────────────────────────────────────────

    def find_match(
        self, embedding: np.ndarray
    ) -> Optional[Tuple[str, str, float]]:
        """Find the closest known face above the similarity threshold.

        Returns ``(face_id, name, score)`` or ``None`` if no match.
        """
        rows = self._conn.execute(
            "SELECT fe.face_id, fe.embedding, f.name "
            "FROM face_embeddings fe JOIN faces f ON fe.face_id = f.id"
        ).fetchall()

        best_score = -1.0
        best_id = None
        best_name = None

        for row in rows:
            stored = np.frombuffer(row["embedding"], dtype=np.float32)
            if stored.shape[0] != _EMBED_DIM:
                continue
            score = float(np.dot(embedding, stored))  # both L2-normalised
            if score > best_score:
                best_score = score
                best_id = row["face_id"]
                best_name = row["name"]

        if best_score >= self._threshold and best_id is not None:
            return best_id, best_name, best_score
        return None

    def register(self, embedding: np.ndarray) -> Tuple[str, str]:
        """Create a new identity and store its first embedding.

        Returns ``(face_id, auto_name)`` where auto_name is "Guest N".
        """
        now = time.time()
        face_id = str(uuid.uuid4())
        auto_name = self._next_guest_name()

        self._conn.execute(
            "INSERT INTO faces (id, name, first_seen, last_seen, last_greeted, seen_count) "
            "VALUES (?, ?, ?, ?, 0, 1)",
            (face_id, auto_name, now, now),
        )
        self._conn.execute(
            "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), face_id, embedding.tobytes(), now),
        )
        self._conn.commit()
        log.info("Registered new face %s as %r", face_id[:8], auto_name)
        return face_id, auto_name

    def set_name(self, face_id: str, name: str) -> bool:
        """Assign or update the name for a known face.  Returns True on success."""
        cur = self._conn.execute(
            "UPDATE faces SET name = ? WHERE id = ?", (name, face_id)
        )
        self._conn.commit()
        if cur.rowcount:
            log.info("Named face %s → %r", face_id[:8], name)
            return True
        log.warning("set_name: face_id %r not found", face_id)
        return False

    def update_seen(self, face_id: str) -> None:
        """Bump last_seen and seen_count for an existing identity."""
        self._conn.execute(
            "UPDATE faces SET last_seen = ?, seen_count = seen_count + 1 WHERE id = ?",
            (time.time(), face_id),
        )
        self._conn.commit()

    def needs_greeting(self, face_id: str, cooldown_s: float = 300.0) -> bool:
        """True if this person hasn't been greeted for at least *cooldown_s* seconds."""
        row = self._conn.execute(
            "SELECT last_greeted FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        if row is None:
            return False
        return (time.time() - row["last_greeted"]) >= cooldown_s

    def mark_greeted(self, face_id: str) -> None:
        """Record that this person was just greeted."""
        self._conn.execute(
            "UPDATE faces SET last_greeted = ? WHERE id = ?",
            (time.time(), face_id),
        )
        self._conn.commit()

    def get_face(self, face_id: str) -> Optional[dict]:
        """Return a dict with all face metadata, or None."""
        row = self._conn.execute(
            "SELECT * FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_current_face_id(self) -> Optional[str]:
        """Return the most recently seen face_id (useful for CLI name assignment)."""
        row = self._conn.execute(
            "SELECT id FROM faces ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def add_embedding(self, face_id: str, embedding: np.ndarray) -> None:
        """Add an additional embedding sample for an existing identity."""
        self._conn.execute(
            "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), face_id, embedding.tobytes(), time.time()),
        )
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # ── Internal ─────────────────────────────────────────────────────────

    def _next_guest_name(self) -> str:
        row = self._conn.execute(
            "SELECT COUNT(*) as n FROM faces WHERE name LIKE 'Guest %'"
        ).fetchone()
        n = (row["n"] if row else 0) + 1
        return f"Guest {n}"
