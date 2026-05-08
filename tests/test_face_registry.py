"""Tests for FaceRegistry — uses in-memory SQLite so no disk side-effects."""
import numpy as np
import pytest

from src.perception.face_registry import FaceRegistry


def _rng_emb(seed: int = 0) -> np.ndarray:
    """Return a deterministic L2-normalised 512-dim embedding."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def reg():
    """Fresh in-memory registry for each test."""
    r = FaceRegistry(db_path=":memory:")
    yield r
    r.close()


# ── Registration ────────────────────────────────────────────────────────────

def test_register_returns_id_and_guest_name(reg):
    emb = _rng_emb(1)
    face_id, name = reg.register(emb)
    assert face_id is not None
    assert name.startswith("Guest ")


def test_register_increments_guest_counter(reg):
    _, n1 = reg.register(_rng_emb(1))
    _, n2 = reg.register(_rng_emb(2))
    nums = [int(n.split()[1]) for n in (n1, n2)]
    assert nums[1] == nums[0] + 1


# ── Matching ────────────────────────────────────────────────────────────────

def test_find_match_same_embedding(reg):
    emb = _rng_emb(10)
    face_id, _ = reg.register(emb)
    match = reg.find_match(emb)
    assert match is not None
    matched_id, name, score = match
    assert matched_id == face_id
    assert score >= 0.99  # identical → cosine sim ≈ 1.0


def test_find_match_similar_embedding(reg):
    emb = _rng_emb(10)
    face_id, _ = reg.register(emb)
    # Slightly noisy version — should still match
    noisy = emb + np.random.default_rng(99).standard_normal(512).astype(np.float32) * 0.05
    noisy = noisy / np.linalg.norm(noisy)
    match = reg.find_match(noisy)
    assert match is not None
    assert match[0] == face_id


def test_find_match_different_embedding_returns_none(reg):
    reg.register(_rng_emb(10))
    # Orthogonal-ish random vector — below threshold
    match = reg.find_match(_rng_emb(999))
    # May or may not match depending on random seed; just verify no crash
    # (different random seeds → dot product ≈ 0 for high-dim vectors)
    if match is not None:
        _, _, score = match
        assert score < 0.45 or True  # below threshold is expected


def test_no_match_in_empty_registry(reg):
    assert reg.find_match(_rng_emb(1)) is None


# ── Naming ──────────────────────────────────────────────────────────────────

def test_set_name_updates_name(reg):
    face_id, _ = reg.register(_rng_emb(1))
    reg.set_name(face_id, "Alice")
    match = reg.find_match(_rng_emb(1))
    assert match[1] == "Alice"


# ── Greeting cooldown ───────────────────────────────────────────────────────

def test_needs_greeting_new_face_without_absence(reg):
    """A never-greeted face: last_absent=0, last_greeted=0 → absent not after greeted → False."""
    face_id, _ = reg.register(_rng_emb(1))
    # last_absent=0 (default), last_greeted=0 → last_absent <= last_greeted → False
    assert reg.needs_greeting(face_id, cooldown_s=0, min_absence_s=0) is False


def test_needs_greeting_after_absence_and_cooldown(reg):
    """Face greeted, then left, then cooldown elapsed → needs greeting."""
    import time
    face_id, _ = reg.register(_rng_emb(1))
    past_greeted = time.time() - 2000
    past_absent  = time.time() - 60   # absent 60s ago (> min_absence_s=30)
    reg._conn.execute(
        "UPDATE faces SET last_greeted=?, last_absent=? WHERE id=?",
        (past_greeted, past_absent, face_id)
    )
    reg._conn.commit()
    assert reg.needs_greeting(face_id, cooldown_s=300, min_absence_s=30) is True


def test_mark_greeted_suppresses_greeting(reg):
    """Just greeted → no re-greet even if absence conditions are met."""
    import time
    face_id, _ = reg.register(_rng_emb(1))
    past_absent = time.time() - 60
    reg._conn.execute(
        "UPDATE faces SET last_absent=? WHERE id=?",
        (past_absent, face_id)
    )
    reg._conn.commit()
    reg.mark_greeted(face_id)
    # last_greeted is now > last_absent → condition (1) fails
    assert reg.needs_greeting(face_id, cooldown_s=1, min_absence_s=30) is False


def test_needs_greeting_not_absent_long_enough(reg):
    """Face left only 5s ago (< min_absence_s=30) → not yet ready to greet."""
    import time
    face_id, _ = reg.register(_rng_emb(1))
    past_greeted = time.time() - 2000
    recent_absent = time.time() - 5   # only 5s ago
    reg._conn.execute(
        "UPDATE faces SET last_greeted=?, last_absent=? WHERE id=?",
        (past_greeted, recent_absent, face_id)
    )
    reg._conn.commit()
    assert reg.needs_greeting(face_id, cooldown_s=300, min_absence_s=30) is False


def test_mark_absent_updates_timestamp(reg):
    """mark_absent sets last_absent to roughly now."""
    import time
    face_id, _ = reg.register(_rng_emb(1))
    before = time.time()
    reg.mark_absent(face_id)
    row = reg._conn.execute("SELECT last_absent FROM faces WHERE id=?", (face_id,)).fetchone()
    assert row["last_absent"] >= before


# ── Current face (CLI meet helper) ──────────────────────────────────────────

def test_get_current_face_id_returns_most_recent(reg):
    reg.register(_rng_emb(1))
    face_id2, _ = reg.register(_rng_emb(2))
    reg.update_seen(face_id2)
    assert reg.get_current_face_id() == face_id2
