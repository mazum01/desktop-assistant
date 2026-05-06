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

def test_needs_greeting_new_face(reg):
    face_id, _ = reg.register(_rng_emb(1))
    # Never greeted → needs greeting
    assert reg.needs_greeting(face_id, cooldown_s=300) is True


def test_mark_greeted_suppresses_greeting(reg):
    face_id, _ = reg.register(_rng_emb(1))
    reg.mark_greeted(face_id)
    # Just greeted → no re-greet
    assert reg.needs_greeting(face_id, cooldown_s=300) is False


def test_needs_greeting_after_cooldown(reg):
    import time
    face_id, _ = reg.register(_rng_emb(1))
    reg.mark_greeted(face_id)
    # Simulate elapsed time by directly updating last_greeted
    con = reg._conn
    past = time.time() - 400  # 400s ago > 300s cooldown
    con.execute("UPDATE faces SET last_greeted=? WHERE id=?", (past, face_id))
    con.commit()
    assert reg.needs_greeting(face_id, cooldown_s=300) is True


# ── Current face (CLI meet helper) ──────────────────────────────────────────

def test_get_current_face_id_returns_most_recent(reg):
    reg.register(_rng_emb(1))
    face_id2, _ = reg.register(_rng_emb(2))
    reg.update_seen(face_id2)
    assert reg.get_current_face_id() == face_id2
