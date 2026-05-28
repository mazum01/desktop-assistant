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
_MATCH_THRESHOLD = 0.60      # cosine similarity ≥ this → confirmed same person (raised from 0.50)
_TENTATIVE_LOW   = 0.40      # cosine similarity ≥ this → tentative match (avoid Guest creation)
_MATCH_MARGIN    = 0.10      # winning identity must lead runner-up by at least this
_GUEST_MERGE_MIN = 0.75      # auto-merge new Guest if existing Guest scores ≥ this
_NEW_IDENTITY_MAX_SIM = 0.50 # cosine to ALL existing identities must be below this to allow Guest creation
_EMBED_DIM = 512
_EMBED_CAP = 20              # max embeddings stored per identity (sliding window)
_AGG_TOP_K = 3               # mean of top-K individual scores per identity during matching
_QUALITY_GATE_MIN_FRAMES = 5 # minimum stored embeddings before quality-gating new arrivals
_QUALITY_GATE_MIN_SIM = 0.45 # reject new embedding if < this similar to identity centroid (raised from 0.30 — 0.30 was too permissive and allowed contamination of identities like Mark)


def _is_guest_name(name: str | None) -> bool:
    """Return True when *name* is an auto-generated Guest placeholder."""
    return bool(name) and name.startswith("Guest ")


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
        # In-memory embedding cache: per-row individual vectors (N, 512) plus an identity
        # index that groups row indices by face_id for fast per-identity scoring.
        # Matching uses mean-of-top-K individual scores per identity; more robust than a
        # single prototype vector because outlier embeddings cannot win on their own.
        self._emb_matrix: Optional[np.ndarray] = None   # (N, 512) — all individual embeddings
        self._emb_face_ids: list = []                    # parallel list of face_id strings
        self._emb_names: list = []                       # parallel list of name strings
        self._emb_row_ids: list = []                     # parallel list of embedding row UUIDs
        # Per-identity index — maintained incrementally; rebuilt on full cache build.
        self._identity_ids: list = []                    # ordered unique face_id strings
        self._identity_names: list = []                  # name per identity slot
        self._identity_slices: list = []                 # list[list[int]] — row indices into _emb_matrix
        self._id_to_slot: dict = {}                      # face_id → slot index in _identity_* lists
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

        Scores every stored embedding individually (one BLAS matmul), then
        aggregates per identity as the mean of the top-``_AGG_TOP_K`` scores.
        This is more robust than comparing against a single prototype vector:
        an outlier embedding in the gallery cannot win on its own — the identity
        must score consistently across multiple stored samples.

        Additionally requires the winner to lead the runner-up by at least
        ``_MATCH_MARGIN`` cosine similarity points.  Ambiguous scores (two
        identities too close together) return ``None`` rather than committing
        to a guess, preventing cross-identity confusion.

        Returns ``(face_id, name, score)`` or ``None`` if no match.
        """
        if self._emb_matrix is None:
            self._build_emb_cache()

        mat = self._emb_matrix
        if mat is None or mat.shape[0] == 0:
            return None

        scores = mat @ embedding          # (N,) — one BLAS call over all individual rows

        # Per-identity mean-of-top-K aggregation. Collect ALL identities so we can
        # apply named-vs-guest tie-breaking later (a "Guest" entry that's close to
        # a named identity is almost always the same person — never let a Guest
        # outrank a named identity on a margin tie).
        ranked: list[tuple[float, str, str]] = []   # (agg_score, face_id, name)
        for fid, name, idxs in zip(
            self._identity_ids, self._identity_names, self._identity_slices
        ):
            if not idxs:
                continue
            face_scores = scores[idxs]
            k = min(_AGG_TOP_K, len(face_scores))
            agg = float(np.mean(np.partition(face_scores, -k)[-k:]))
            ranked.append((agg, fid, name))

        if not ranked:
            return None
        ranked.sort(key=lambda t: -t[0])
        best_score, best_id, best_name = ranked[0]
        second_best = ranked[1][0] if len(ranked) > 1 else -1.0

        # Named-over-Guest preference: if the top scorer is a Guest but a named
        # identity is within margin AND above the threshold, prefer the named one.
        # Guest entries are auto-generated placeholders; named identities were
        # intentionally created by the user and should win ties.
        if best_score >= self._threshold and _is_guest_name(best_name):
            for s, fid, name in ranked[1:]:
                if s < self._threshold:
                    break
                if not _is_guest_name(name) and best_score - s < _MATCH_MARGIN:
                    log.debug(
                        "find_match: prefer named %r (%.3f) over Guest %r (%.3f)",
                        name, s, best_name, best_score,
                    )
                    best_score, best_id, best_name = s, fid, name
                    # Recompute runner-up excluding the chosen one
                    second_best = max(
                        (ss for ss, _, _ in ranked if (ss, _, _) != (best_score, best_id, best_name)),
                        default=-1.0,
                    )
                    break

        if best_score >= self._threshold and best_id is not None:
            margin = best_score - second_best
            if margin < _MATCH_MARGIN:
                # Margin failure: if the runner-up is a Guest and the winner is named
                # (or vice versa), we ALREADY preferred named above; here both are
                # the same type. Still return the winner — refusing to identify
                # causes the perception pipeline to create yet another Guest, which
                # makes the gallery contamination problem worse on every frame.
                # Only return None when BOTH competing identities are NAMED (a real
                # ambiguity that needs more data to resolve).
                runner_up_name = None
                for s, _fid, n in ranked[1:]:
                    if abs(s - second_best) < 1e-6:
                        runner_up_name = n
                        break
                both_named = (
                    runner_up_name is not None
                    and not _is_guest_name(best_name)
                    and not _is_guest_name(runner_up_name)
                )
                if both_named:
                    log.debug(
                        "find_match: ambiguous between named identities %r=%.3f and %r=%.3f",
                        best_name, best_score, runner_up_name, second_best,
                    )
                    return None
                # Otherwise, return the winner — refusing to identify here is what
                # was creating Guest-floods (every margin failure → new Guest of
                # the same person → next frame even more ambiguous).
                log.debug(
                    "find_match: low margin (%.3f) but returning %r=%.3f (avoid Guest cascade)",
                    margin, best_name, best_score,
                )
            return best_id, best_name, best_score
        return None

    def find_tentative_match(
        self, embedding: np.ndarray
    ) -> Optional[Tuple[str, str, float]]:
        """Return the best identity match in the tentative zone [_TENTATIVE_LOW, threshold).

        Called when ``find_match`` returns None.  A tentative match means the
        embedding is somewhat plausible for a known identity but not confident
        enough to commit.  The caller should continue accumulating stabilisation
        votes rather than immediately registering a new Guest.

        Applies the same margin check as ``find_match``.
        Returns ``(face_id, name, score)`` or ``None``.
        """
        if self._emb_matrix is None:
            self._build_emb_cache()

        mat = self._emb_matrix
        if mat is None or mat.shape[0] == 0:
            return None

        scores = mat @ embedding

        ranked: list[tuple[float, str, str]] = []
        for fid, name, idxs in zip(
            self._identity_ids, self._identity_names, self._identity_slices
        ):
            if not idxs:
                continue
            face_scores = scores[idxs]
            k = min(_AGG_TOP_K, len(face_scores))
            agg = float(np.mean(np.partition(face_scores, -k)[-k:]))
            ranked.append((agg, fid, name))

        if not ranked:
            return None
        ranked.sort(key=lambda t: -t[0])
        best_score, best_id, best_name = ranked[0]
        second_best = ranked[1][0] if len(ranked) > 1 else -1.0

        # Named-over-Guest preference (same logic as find_match)
        if _TENTATIVE_LOW <= best_score < self._threshold and _is_guest_name(best_name):
            for s, fid, name in ranked[1:]:
                if s < _TENTATIVE_LOW:
                    break
                if not _is_guest_name(name) and best_score - s < _MATCH_MARGIN:
                    best_score, best_id, best_name = s, fid, name
                    break

        if _TENTATIVE_LOW <= best_score < self._threshold and best_id is not None:
            # Return the best candidate even on small margins — see find_match for
            # rationale (refusing only causes Guest-cascade).
            return best_id, best_name, best_score
        return None

    def _build_emb_cache(self) -> None:
        """Load all embeddings from DB and build the in-memory cache and identity index."""
        rows = self._conn.execute(
            "SELECT fe.id, fe.face_id, fe.embedding, f.name "
            "FROM face_embeddings fe JOIN faces f ON fe.face_id = f.id "
            "ORDER BY fe.created_at ASC"
        ).fetchall()
        vecs: list = []
        ids: list = []
        names: list = []
        row_ids: list = []
        identity_ids: list = []
        identity_names: list = []
        identity_slices: list = []
        id_to_slot: dict = {}
        for row in rows:
            stored = np.frombuffer(row["embedding"], dtype=np.float32)
            if stored.shape[0] != _EMBED_DIM:
                continue
            mat_idx = len(vecs)
            vecs.append(stored)
            fid = row["face_id"]
            ids.append(fid)
            names.append(row["name"])
            row_ids.append(row["id"])
            if fid not in id_to_slot:
                id_to_slot[fid] = len(identity_ids)
                identity_ids.append(fid)
                identity_names.append(row["name"])
                identity_slices.append([])
            identity_slices[id_to_slot[fid]].append(mat_idx)
        self._emb_matrix = np.stack(vecs, axis=0) if vecs else np.empty((0, _EMBED_DIM), dtype=np.float32)
        self._emb_face_ids = ids
        self._emb_names = names
        self._emb_row_ids = row_ids
        self._identity_ids = identity_ids
        self._identity_names = identity_names
        self._identity_slices = identity_slices
        self._id_to_slot = id_to_slot


    def _invalidate_emb_cache(self) -> None:
        self._emb_matrix = None
        self._emb_face_ids = []
        self._emb_names = []
        self._emb_row_ids = []
        self._identity_ids = []
        self._identity_names = []
        self._identity_slices = []
        self._id_to_slot = {}

    def reload(self) -> None:
        """Invalidate the in-memory embedding cache so it is rebuilt from the DB on the next match."""
        self._invalidate_emb_cache()

    def _append_to_cache(
        self, row_id: Optional[str], face_id: str, name: str, embedding: np.ndarray
    ) -> None:
        """Append one embedding row to the in-memory cache without a full rebuild.

        No-op if the cache hasn't been built yet (will be built lazily on next
        find_match call).  When *row_id* is None (zero/blurry embedding not
        persisted) only the identity slot is registered — no matrix row is added.
        """
        if self._emb_matrix is None:
            return

        # Register identity slot even if we have no valid embedding yet.
        if face_id not in self._id_to_slot:
            slot = len(self._identity_ids)
            self._id_to_slot[face_id] = slot
            self._identity_ids.append(face_id)
            self._identity_names.append(name)
            self._identity_slices.append([])

        if row_id is None or np.all(embedding == 0):
            return  # identity slot registered; no matrix row to add

        vec = embedding.astype(np.float32)
        if vec.shape[0] != _EMBED_DIM:
            return
        mat_idx = self._emb_matrix.shape[0]
        self._emb_matrix = np.vstack([self._emb_matrix, vec[np.newaxis, :]])
        self._emb_face_ids.append(face_id)
        self._emb_names.append(name)
        self._emb_row_ids.append(row_id)
        self._identity_slices[self._id_to_slot[face_id]].append(mat_idx)

    def _replace_in_cache(
        self, old_row_id: str, new_row_id: str, face_id: str, name: str, embedding: np.ndarray
    ) -> None:
        """Replace one embedding row in-place (prune-and-replace path).

        Finds *old_row_id* by index and overwrites it with the new vector — no
        array reallocation needed since the matrix shape stays the same.  The
        identity index is unchanged because the same slot (matrix row index) is
        reused; no slice update is needed.
        No-op if the cache isn't built or the old row isn't found.
        """
        if self._emb_matrix is None:
            return
        try:
            idx = self._emb_row_ids.index(old_row_id)
        except ValueError:
            # Old row wasn't in cache; just append instead
            self._append_to_cache(new_row_id, face_id, name, embedding)
            return
        vec = embedding.astype(np.float32)
        if vec.shape[0] != _EMBED_DIM:
            return
        self._emb_matrix[idx] = vec
        self._emb_row_ids[idx] = new_row_id
        # face_id and name unchanged; identity_slices already points at idx

    def register(self, embedding: np.ndarray) -> Tuple[str, str]:
        """Create a new Guest identity (or merge into an existing similar Guest).

        Before creating a new row, scans all existing identities (Guests AND
        named) for cosine similarity ≥ ``_GUEST_MERGE_MIN``.  If found, the
        incoming embedding is merged into that identity instead of spawning a
        new one.  This prevents the same person from accumulating multiple
        "Guest N" entries across re-appearances.  Critically, this includes
        merging into NAMED identities — without this, a known person whose
        live embedding is borderline can spawn a Guest "twin" that then competes
        with the named identity on every future frame (the Guest-cascade bug).

        Also gates Guest creation: if the embedding has cosine ≥
        ``_NEW_IDENTITY_MAX_SIM`` to ANY existing identity but didn't qualify
        for auto-merge, the registration is REFUSED — the perception layer
        will then keep stabilising rather than polluting the gallery.

        Returns ``(face_id, auto_name)`` — either the merged identity or the
        newly created one.  Returns ``(existing_id, name)`` if merged.
        """
        # Scan all existing identities for an auto-merge candidate.
        if not np.all(embedding == 0):
            if self._emb_matrix is None:
                self._build_emb_cache()
            if self._emb_matrix is not None and self._emb_matrix.shape[0] > 0:
                scores = self._emb_matrix @ embedding
                best_named: tuple[float, str, str] | None = None
                best_guest: tuple[float, str, str] | None = None
                for fid, name, idxs in zip(
                    self._identity_ids, self._identity_names, self._identity_slices
                ):
                    if not idxs:
                        continue
                    k = min(_AGG_TOP_K, len(idxs))
                    agg = float(np.mean(np.partition(scores[idxs], -k)[-k:]))
                    if _is_guest_name(name):
                        if best_guest is None or agg > best_guest[0]:
                            best_guest = (agg, fid, name)
                    else:
                        if best_named is None or agg > best_named[0]:
                            best_named = (agg, fid, name)

                # Prefer named identity merge (always — a named identity match
                # at the merge threshold means this is the same person).
                if best_named is not None and best_named[0] >= _GUEST_MERGE_MIN:
                    agg, fid, name = best_named
                    log.info(
                        "register: auto-merged into NAMED identity %s (%r, sim=%.3f) — preventing Guest twin",
                        fid[:8], name, agg,
                    )
                    self.update_seen(fid)
                    self.add_embedding_if_needed(fid, embedding)
                    return fid, name

                if best_guest is not None and best_guest[0] >= _GUEST_MERGE_MIN:
                    agg, fid, name = best_guest
                    log.info(
                        "register: merged new embedding into existing Guest %s (%r, sim=%.3f)",
                        fid[:8], name, agg,
                    )
                    self.update_seen(fid)
                    self.add_embedding_if_needed(fid, embedding)
                    return fid, name

                # Strict gate: if ANY existing identity is "similar but not merge-worthy",
                # refuse to create a new Guest. The perception layer will keep stabilising;
                # eventually we'll get either a clearer embedding (→ match) or sustained
                # truly-unknown frames (→ all identities below threshold → safe to register).
                top_sim = max(
                    (best_named[0] if best_named else -1.0),
                    (best_guest[0] if best_guest else -1.0),
                )
                if top_sim >= _NEW_IDENTITY_MAX_SIM:
                    closest = best_named or best_guest
                    if closest is not None:
                        log.debug(
                            "register: refusing new Guest — closest identity %r at sim=%.3f ≥ %.2f gate",
                            closest[2], top_sim, _NEW_IDENTITY_MAX_SIM,
                        )
                    # Return the closest existing identity so the caller has SOMETHING
                    # to label this face as. Marking it seen reinforces good gating.
                    if closest is not None:
                        self.update_seen(closest[1])
                        return closest[1], closest[2]

        now = time.time()
        face_id = str(uuid.uuid4())
        auto_name = self._next_guest_name()

        self._conn.execute(
            "INSERT INTO faces (id, name, first_seen, last_seen, last_greeted, last_absent, seen_count) "
            "VALUES (?, ?, ?, ?, 0, 0, 1)",
            (face_id, auto_name, now, now),
        )
        # Only persist the initial embedding if it is non-zero (i.e. not a blurry/failed crop).
        # A Guest with no stored embedding will accumulate one once a sharp frame is captured.
        if not np.all(embedding == 0):
            row_id = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
                "VALUES (?, ?, ?, ?)",
                (row_id, face_id, embedding.tobytes(), now),
            )
            self._conn.commit()
            self._append_to_cache(row_id, face_id, auto_name, embedding)
        else:
            self._conn.commit()
            # Still need a cache slot for this identity (no embedding yet).
            self._append_to_cache(None, face_id, auto_name, embedding)
        log.info("Registered new face %s as %r (has_embedding=%s)",
                 face_id[:8], auto_name, not np.all(embedding == 0))
        return face_id, auto_name

    def set_name(self, face_id: str, name: str) -> bool:
        """Assign or update the name for a known face.  Returns True on success."""
        cur = self._conn.execute(
            "UPDATE faces SET name = ? WHERE id = ?", (name, face_id)
        )
        self._conn.commit()
        if cur.rowcount:
            # Update names in-place in both flat and identity caches.
            for i, fid in enumerate(self._emb_face_ids):
                if fid == face_id:
                    self._emb_names[i] = name
            if face_id in self._id_to_slot:
                self._identity_names[self._id_to_slot[face_id]] = name
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

    def mark_all_absent(self) -> None:
        """Stamp every known face as absent right now.

        Called at service startup so that any face already in frame when
        the daemon (re)starts is treated as a returning visitor and greeted,
        rather than being silently skipped because last_absent never advanced
        past last_greeted from the previous session.
        """
        self._conn.execute("UPDATE faces SET last_absent = ?", (time.time(),))
        self._conn.commit()
        log.debug("FaceRegistry: marked all faces absent (service restart)")

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

    def delete_guest_faces(self) -> tuple[int, list[str]]:
        """Delete all faces whose name starts with 'Guest '.

        Removes their embeddings and thumbnails too.
        Returns ``(count, [deleted_face_ids])``.
        """
        rows = self._conn.execute(
            "SELECT id FROM faces WHERE name LIKE 'Guest %'"
        ).fetchall()
        deleted_ids = [row["id"] for row in rows]
        for fid in deleted_ids:
            self._conn.execute("DELETE FROM face_embeddings WHERE face_id = ?", (fid,))
            self._conn.execute("DELETE FROM faces WHERE id = ?", (fid,))
            self.delete_thumbnail(fid)
        self._conn.commit()
        self._invalidate_emb_cache()
        log.info("Deleted %d guest face(s) from registry", len(deleted_ids))
        return len(deleted_ids), deleted_ids

    def find_match_by_crop(
        self, crop: np.ndarray, threshold: float = 0.75
    ) -> Optional[Tuple[str, str, float]]:
        """Find the best-matching face by comparing *crop* against stored thumbnails.

        Uses HSV histogram correlation — fast, zero extra deps, works in sim mode
        when embedding vectors are all-zero.

        Returns ``(face_id, name, score)`` or ``None`` if no match above *threshold*.
        Correlation ranges −1 … 1; threshold raised to 0.75 (from 0.60) to reduce
        false positives between different people with similar skin tones.
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
        self._invalidate_emb_cache()
        log.info("Merged face %s into %s (%r)", absorb_id[:8], keep_id[:8], keep["name"])
        return True

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
        self._invalidate_emb_cache()
        log.info("Deleted all %d face(s) from registry", count)
        return count

    def delete_face(self, face_id: str) -> bool:
        """Remove a face and all its embeddings. Returns True if found."""
        cur = self._conn.execute("DELETE FROM face_embeddings WHERE face_id = ?", (face_id,))
        cur2 = self._conn.execute("DELETE FROM faces WHERE id = ?", (face_id,))
        self._conn.commit()
        self._invalidate_emb_cache()
        self.delete_thumbnail(face_id)
        return cur2.rowcount > 0

    def prune_gallery(self, min_sim: float = _QUALITY_GATE_MIN_SIM, cap: int = _EMBED_CAP) -> int:
        """Retroactively apply the quality gate and cap to all stored embeddings.

        For each identity:
        1. Computes the centroid and removes any embedding whose cosine similarity
           falls below *min_sim*.
        2. Enforces *cap*: if more than *cap* embeddings remain after quality
           pruning, keeps only the most recent *cap* embeddings.

        At least ``_QUALITY_GATE_MIN_FRAMES`` embeddings are always preserved per
        identity (the best-scoring ones), so no identity is ever emptied.

        Returns the total number of embeddings removed.
        """
        removed = 0
        face_ids = [r["id"] for r in self._conn.execute("SELECT id FROM faces").fetchall()]
        for face_id in face_ids:
            rows = self._conn.execute(
                "SELECT id, embedding, created_at FROM face_embeddings WHERE face_id = ? "
                "ORDER BY created_at ASC",
                (face_id,),
            ).fetchall()
            if len(rows) < _QUALITY_GATE_MIN_FRAMES:
                continue  # too few embeddings to prune safely
            embs = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
            centroid = embs.mean(axis=0).astype(np.float32)
            norm = np.linalg.norm(centroid)
            if norm < 1e-10:
                continue
            centroid /= norm
            sims = embs @ centroid  # cosine similarity of each row to the centroid
            keep_mask = sims >= min_sim
            if keep_mask.sum() < _QUALITY_GATE_MIN_FRAMES:
                # Never nuke an identity — always preserve the best MIN_FRAMES embeddings
                top_idx = np.argpartition(sims, -_QUALITY_GATE_MIN_FRAMES)[-_QUALITY_GATE_MIN_FRAMES:]
                keep_mask = np.zeros(len(rows), dtype=bool)
                keep_mask[top_idx] = True

            # Apply cap: if too many qualify, keep only the most recent *cap* embeddings.
            # rows is already sorted ASC by created_at, so the newest are at the end.
            if keep_mask.sum() > cap:
                keep_indices = np.where(keep_mask)[0]
                evict = keep_indices[:-cap]  # drop the oldest among the keepers
                keep_mask[evict] = False

            for i, row in enumerate(rows):
                if not keep_mask[i]:
                    self._conn.execute("DELETE FROM face_embeddings WHERE id = ?", (row["id"],))
                    removed += 1
        if removed:
            self._conn.commit()
            self._invalidate_emb_cache()
            log.info("prune_gallery: removed %d outlier/excess embedding(s)", removed)
        return removed

    def clear_embeddings(self, face_id: str) -> int:
        """Remove all stored embeddings for *face_id* without deleting the face entry.

        The face's name, timestamps, and seen_count are preserved.  On next
        detection the identity will be re-enrolled from scratch under the new
        quality gate, producing a clean gallery.  Returns the number of
        embeddings removed, or -1 if the face_id does not exist.
        """
        if not self._conn.execute("SELECT 1 FROM faces WHERE id = ?", (face_id,)).fetchone():
            return -1
        cur = self._conn.execute("DELETE FROM face_embeddings WHERE face_id = ?", (face_id,))
        self._conn.commit()
        self._invalidate_emb_cache()
        log.info("clear_embeddings: removed %d embedding(s) for face %s", cur.rowcount, face_id[:8])
        return cur.rowcount

    def get_current_face_id(self) -> Optional[str]:
        """Return the most recently seen face_id (useful for CLI name assignment)."""
        row = self._conn.execute(
            "SELECT id FROM faces ORDER BY last_seen DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    def add_embedding(self, face_id: str, embedding: np.ndarray) -> None:
        """Add an additional embedding sample for an existing identity."""
        row_id = str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (row_id, face_id, embedding.tobytes(), time.time()),
        )
        self._conn.commit()
        # Look up current name from cache (avoid DB round-trip)
        name = next(
            (self._emb_names[i] for i, fid in enumerate(self._emb_face_ids) if fid == face_id),
            "?",
        )
        self._append_to_cache(row_id, face_id, name, embedding)

    def add_embedding_if_needed(
        self, face_id: str, embedding: np.ndarray, cap: int = _EMBED_CAP
    ) -> bool:
        """Add a new embedding sample for *face_id* unless the cap is reached.

        Old embeddings are pruned (oldest first) to keep the total ≤ *cap*.
        Updates the in-memory cache incrementally — no full SQLite rebuild.
        Returns True if an embedding was added.
        """
        if np.all(embedding == 0):
            return False

        # Build cache lazily so the quality gate below can inspect existing embeddings.
        if self._emb_matrix is None:
            self._build_emb_cache()

        # Quality gate: if we have enough stored embeddings for this identity, reject
        # new ones that are too dissimilar from the current cluster centroid.
        # This prevents garbage captures (side profile, occlusion, blur) from
        # polluting the gallery and degrading match scores.
        if self._emb_matrix is not None and face_id in self._id_to_slot:
            idxs = self._identity_slices[self._id_to_slot[face_id]]
            if len(idxs) >= _QUALITY_GATE_MIN_FRAMES:
                recent = idxs[-10:]
                centroid = self._emb_matrix[recent].mean(axis=0).astype(np.float32)
                norm = np.linalg.norm(centroid)
                if norm > 1e-10:
                    centroid /= norm
                    sim = float(centroid @ embedding)
                    if sim < _QUALITY_GATE_MIN_SIM:
                        log.debug(
                            "add_embedding_if_needed(%s): rejected (sim=%.3f < %.2f)",
                            face_id[:8], sim, _QUALITY_GATE_MIN_SIM,
                        )
                        return False
        rows = self._conn.execute(
            "SELECT id, created_at FROM face_embeddings WHERE face_id = ? "
            "ORDER BY created_at ASC",
            (face_id,),
        ).fetchall()
        new_row_id = str(uuid.uuid4())
        at_cap = len(rows) >= cap
        oldest_id: Optional[str] = None
        if at_cap:
            oldest_id = rows[0]["id"]
            self._conn.execute(
                "DELETE FROM face_embeddings WHERE id = ?", (oldest_id,)
            )
        self._conn.execute(
            "INSERT INTO face_embeddings (id, face_id, embedding, created_at) "
            "VALUES (?, ?, ?, ?)",
            (new_row_id, face_id, embedding.tobytes(), time.time()),
        )
        self._conn.commit()
        # Update cache incrementally — no full rebuild
        name = next(
            (self._emb_names[i] for i, fid in enumerate(self._emb_face_ids) if fid == face_id),
            "?",
        )
        if at_cap and oldest_id is not None:
            self._replace_in_cache(oldest_id, new_row_id, face_id, name, embedding)
        else:
            self._append_to_cache(new_row_id, face_id, name, embedding)
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
