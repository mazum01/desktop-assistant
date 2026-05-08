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
    last_absent  REAL       Unix timestamp when face last left the camera frame (0 = never)
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
_DEFAULT_THUMBS = Path.home() / ".local" / "share" / "desktop-assistant" / "thumbs"
_MATCH_THRESHOLD = 0.40      # cosine similarity ≥ this → same person
_EMBED_DIM = 512
_EMBED_CAP = 20              # max embeddings stored per identity (sliding window)


class FaceRegistry:
    """Persistent face identity store backed by SQLite.

    Thread-safe for single-writer usage; all public methods acquire the same
    connection on the calling thread (``check_same_thread=False`` so the
    PerceptionService thread can use the same instance created on the main thread).
    """

    def __init__(
        self,
        db_path: Optional[str | Path] = None,
        thumbs_dir: Optional[str | Path] = None,
        match_threshold: float = _MATCH_THRESHOLD,
    ) -> None:
        path = Path(db_path) if db_path else _DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._threshold = match_threshold
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._ensure_schema()
        self._thumbs_dir = Path(thumbs_dir) if thumbs_dir else _DEFAULT_THUMBS
        self._thumbs_dir.mkdir(parents=True, exist_ok=True)
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
                last_absent  REAL NOT NULL DEFAULT 0,
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
        # Migrate existing DBs that lack last_absent
        cols = {row[1] for row in self._conn.execute("PRAGMA table_info(faces)")}
        if "last_absent" not in cols:
            self._conn.execute(
                "ALTER TABLE faces ADD COLUMN last_absent REAL NOT NULL DEFAULT 0"
            )
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
            "INSERT INTO faces (id, name, first_seen, last_seen, last_greeted, last_absent, seen_count) "
            "VALUES (?, ?, ?, ?, 0, 0, 1)",
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

    def needs_greeting(
        self,
        face_id: str,
        cooldown_s: float = 1800.0,
        min_absence_s: float = 30.0,
    ) -> bool:
        """True if this person should be greeted now.

        Conditions (all must be true):
        1. Face was absent from frame since last greeting (last_absent > last_greeted).
        2. Face has been gone for at least *min_absence_s* (debounce brief look-aways).
        3. Cooldown has elapsed since last greeting.
        """
        row = self._conn.execute(
            "SELECT last_greeted, last_absent FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        if row is None:
            return False
        now = time.time()
        last_greeted = row["last_greeted"]
        last_absent  = row["last_absent"]

        # Must have left frame after last greeting
        if last_absent <= last_greeted:
            return False
        # Must have been absent long enough (not just a blink)
        if (now - last_absent) < min_absence_s:
            return False
        # Cooldown since last greeting
        return (now - last_greeted) >= cooldown_s

    def mark_greeted(self, face_id: str) -> None:
        """Record that this person was just greeted."""
        self._conn.execute(
            "UPDATE faces SET last_greeted = ? WHERE id = ?",
            (time.time(), face_id),
        )
        self._conn.commit()

    def mark_absent(self, face_id: str) -> None:
        """Record that this face just disappeared from the camera frame."""
        self._conn.execute(
            "UPDATE faces SET last_absent = ? WHERE id = ?",
            (time.time(), face_id),
        )
        self._conn.commit()

    def get_face(self, face_id: str) -> Optional[dict]:
        """Return a dict with all face metadata, or None."""
        row = self._conn.execute(
            "SELECT * FROM faces WHERE id = ?", (face_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_faces(self) -> list[dict]:
        """Return all known faces sorted by last_seen descending."""
        rows = self._conn.execute(
            "SELECT id, name, first_seen, last_seen, last_greeted, seen_count "
            "FROM faces ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def list_named_faces(self) -> list[dict]:
        """Return only faces with a real name (not 'Guest N'), sorted by last_seen desc."""
        rows = self._conn.execute(
            "SELECT id, name, first_seen, last_seen, last_greeted, seen_count "
            "FROM faces WHERE name NOT LIKE 'Guest %' ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_guest_faces(self) -> int:
        """Delete all faces whose name starts with 'Guest '.

        Removes their embeddings and thumbnails too.
        Returns the number of faces deleted.
        """
        rows = self._conn.execute(
            "SELECT id FROM faces WHERE name LIKE 'Guest %'"
        ).fetchall()
        count = len(rows)
        for row in rows:
            fid = row["id"]
            self._conn.execute("DELETE FROM face_embeddings WHERE face_id = ?", (fid,))
            self._conn.execute("DELETE FROM faces WHERE id = ?", (fid,))
            self.delete_thumbnail(fid)
        self._conn.commit()
        log.info("Deleted %d guest face(s) from registry", count)
        return count

    def find_match_by_crop(
        self, crop: np.ndarray, threshold: float = 0.60
    ) -> Optional[Tuple[str, str, float]]:
        """Find the best-matching face by comparing *crop* against stored thumbnails.

        Uses HSV histogram correlation — fast, zero extra deps, works in sim mode
        when embedding vectors are all-zero.

        Returns ``(face_id, name, score)`` or ``None`` if no match above *threshold*.
        Correlation ranges −1 … 1; values ≥ 0.60 are reliably the same person
        under normal indoor lighting.
        """
        try:
            import cv2 as _cv2
            if crop is None or crop.size == 0:
                return None
            crop_small = _cv2.resize(crop, (64, 64))
            crop_hsv = _cv2.cvtColor(crop_small, _cv2.COLOR_BGR2HSV)
            crop_hist = _cv2.calcHist(
                [crop_hsv], [0, 1], None, [50, 60], [0, 180, 0, 256]
            )
            _cv2.normalize(crop_hist, crop_hist, 0, 1, _cv2.NORM_MINMAX)
        except Exception as exc:
            log.debug("find_match_by_crop: prep failed: %s", exc)
            return None

        rows = self._conn.execute(
            "SELECT id, name FROM faces ORDER BY last_seen DESC"
        ).fetchall()

        best_score = -1.0
        best_id: Optional[str] = None
        best_name: Optional[str] = None

        for row in rows:
            thumb_path = self.thumbnail_path(row["id"])
            if thumb_path is None:
                continue
            try:
                import cv2 as _cv2
                stored = _cv2.imread(str(thumb_path))
                if stored is None:
                    continue
                stored_hsv = _cv2.cvtColor(stored, _cv2.COLOR_BGR2HSV)
                stored_hist = _cv2.calcHist(
                    [stored_hsv], [0, 1], None, [50, 60], [0, 180, 0, 256]
                )
                _cv2.normalize(stored_hist, stored_hist, 0, 1, _cv2.NORM_MINMAX)
                score = float(_cv2.compareHist(
                    crop_hist, stored_hist, _cv2.HISTCMP_CORREL
                ))
                if score > best_score:
                    best_score = score
                    best_id = row["id"]
                    best_name = row["name"]
            except Exception:
                continue

        if best_score >= threshold and best_id is not None:
            log.debug(
                "find_match_by_crop: matched %s (%r) score=%.3f",
                best_id[:8], best_name, best_score,
            )
            return best_id, best_name, best_score
        return None

    def merge_faces(self, keep_id: str, absorb_id: str) -> bool:
        """Merge *absorb_id* into *keep_id*.

        - Reassigns all embeddings from *absorb_id* to *keep_id*.
        - Accumulates *seen_count*.
        - Back-dates *first_seen* if *absorb_id* was seen earlier.
        - Copies *absorb_id*'s thumbnail to *keep_id* if *keep_id* has none.
        - Deletes *absorb_id* entirely.

        Returns True on success, False if either id is not found.
        """
        keep = self.get_face(keep_id)
        absorb = self.get_face(absorb_id)
        if not keep or not absorb:
            log.warning("merge_faces: id not found (keep=%s absorb=%s)", keep_id, absorb_id)
            return False

        self._conn.execute(
            "UPDATE face_embeddings SET face_id = ? WHERE face_id = ?",
            (keep_id, absorb_id),
        )
        if absorb["first_seen"] < keep["first_seen"]:
            self._conn.execute(
                "UPDATE faces SET first_seen = ? WHERE id = ?",
                (absorb["first_seen"], keep_id),
            )
        self._conn.execute(
            "UPDATE faces SET seen_count = seen_count + ? WHERE id = ?",
            (absorb["seen_count"], keep_id),
        )
        # Copy thumbnail (and photo) if keep_id has none
        keep_thumb = self.thumbnail_path(keep_id)
        absorb_thumb = self.thumbnail_path(absorb_id)
        if keep_thumb is None and absorb_thumb is not None:
            import shutil
            shutil.copy2(str(absorb_thumb), str(self._thumbs_dir / f"{keep_id}.jpg"))
        keep_photo = self.photo_path(keep_id)
        absorb_photo = self.photo_path(absorb_id)
        if keep_photo is None and absorb_photo is not None:
            import shutil
            shutil.copy2(str(absorb_photo), str(self._thumbs_dir / f"{keep_id}_photo.jpg"))
        self._conn.execute("DELETE FROM faces WHERE id = ?", (absorb_id,))
        self.delete_thumbnail(absorb_id)
        self._conn.commit()
        log.info("Merged face %s into %s (%r)", absorb_id[:8], keep_id[:8], keep["name"])
        return True
        """Return all known faces sorted by last_seen descending."""
        rows = self._conn.execute(
            "SELECT id, name, first_seen, last_seen, last_greeted, seen_count "
            "FROM faces ORDER BY last_seen DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_all_faces(self) -> int:
        """Remove every face and all embeddings. Returns count of faces deleted."""
        cur = self._conn.execute("SELECT COUNT(*) as n FROM faces").fetchone()
        count = cur["n"] if cur else 0
        # Remove all thumbnails
        for p in self._thumbs_dir.glob("*.jpg"):
            try:
                p.unlink()
            except Exception:
                pass
        self._conn.execute("DELETE FROM face_embeddings")
        self._conn.execute("DELETE FROM faces")
        self._conn.commit()
        log.info("Deleted all %d face(s) from registry", count)
        return count

    def delete_face(self, face_id: str) -> bool:
        """Remove a face and all its embeddings. Returns True if found."""
        cur = self._conn.execute("DELETE FROM face_embeddings WHERE face_id = ?", (face_id,))
        cur2 = self._conn.execute("DELETE FROM faces WHERE id = ?", (face_id,))
        self._conn.commit()
        self.delete_thumbnail(face_id)
        return cur2.rowcount > 0

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

    def add_embedding_if_needed(
        self, face_id: str, embedding: np.ndarray, cap: int = _EMBED_CAP
    ) -> bool:
        """Add a new embedding sample for *face_id* unless the cap is reached.

        Old embeddings are pruned (oldest first) to keep the total ≤ *cap*.
        Returns True if an embedding was added.
        """
        if np.all(embedding == 0):
            return False
        rows = self._conn.execute(
            "SELECT id, created_at FROM face_embeddings WHERE face_id = ? "
            "ORDER BY created_at ASC",
            (face_id,),
        ).fetchall()
        if len(rows) >= cap:
            # Prune oldest to make room
            oldest_id = rows[0]["id"]
            self._conn.execute(
                "DELETE FROM face_embeddings WHERE id = ?", (oldest_id,)
            )
        self._conn.execute(
            "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), face_id, embedding.tobytes(), time.time()),
        )
        self._conn.commit()
        return True

    def save_thumbnail(self, face_id: str, crop: np.ndarray) -> bool:
        """Save a face crop as a JPEG thumbnail (64×64) and a full-size photo.

        The thumbnail (``{face_id}.jpg``) is used for fast list display.
        The photo (``{face_id}_photo.jpg``) is the best-quality crop saved at
        up to 320×320, used for the lightbox full-size view.  Returns True on
        success for the thumbnail write (photo failure is non-fatal).
        """
        try:
            import cv2
            thumb_path = self._thumbs_dir / f"{face_id}.jpg"
            small = cv2.resize(crop, (64, 64), interpolation=cv2.INTER_AREA)
            cv2.imwrite(str(thumb_path), small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        except Exception as exc:
            log.warning("save_thumbnail(%s): %s", face_id[:8], exc)
            return False

        # Save full-size photo (up to 320×320, preserving aspect ratio)
        try:
            import cv2
            photo_path = self._thumbs_dir / f"{face_id}_photo.jpg"
            h, w = crop.shape[:2]
            max_dim = 320
            if h > max_dim or w > max_dim:
                scale = max_dim / max(h, w)
                photo = cv2.resize(
                    crop,
                    (int(w * scale), int(h * scale)),
                    interpolation=cv2.INTER_AREA,
                )
            else:
                photo = crop.copy()
            # Only overwrite if this crop is larger than what we have
            if not photo_path.exists() or max(h, w) >= max_dim:
                cv2.imwrite(str(photo_path), photo, [cv2.IMWRITE_JPEG_QUALITY, 92])
        except Exception as exc:
            log.debug("save_photo(%s): %s", face_id[:8], exc)

        return True

    def photo_path(self, face_id: str) -> Optional[Path]:
        """Return the Path to the full-size photo JPEG, or None if it doesn't exist."""
        p = self._thumbs_dir / f"{face_id}_photo.jpg"
        return p if p.exists() else None

    def thumbnail_path(self, face_id: str) -> Optional[Path]:
        """Return the Path to the thumbnail JPEG, or None if it doesn't exist."""
        p = self._thumbs_dir / f"{face_id}.jpg"
        return p if p.exists() else None

    def delete_thumbnail(self, face_id: str) -> None:
        """Remove the thumbnail and full-size photo for *face_id* if present."""
        for suffix in ("", "_photo"):
            p = self._thumbs_dir / f"{face_id}{suffix}.jpg"
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass

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
