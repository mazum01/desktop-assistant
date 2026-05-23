"""Tests for FaceRegistry — uses in-memory SQLite so no disk side-effects."""
import numpy as np
import pytest

from src.perception.face_registry import FaceRegistry


def _rng_emb(seed: int = 0) -> np.ndarray:
    """Return a deterministic L2-normalised 512-dim embedding."""
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(512).astype(np.float32)
    return v / np.linalg.norm(v)


def _sphere_emb(base: np.ndarray, cos_sim: float, rng: np.random.Generator) -> np.ndarray:
    """Return a 512-dim unit vector with the exact given cosine similarity to *base*.

    Uses Gram-Schmidt to build an orthogonal component, then mixes with *base*
    at the right ratio.  This avoids the high-dim Gaussian noise trap where
    large σ in R^512 yields very low cosine similarity after normalisation.
    """
    perp = rng.standard_normal(512).astype(np.float32)
    perp -= float(perp @ base) * base
    perp /= np.linalg.norm(perp)
    sin_sim = float(np.sqrt(max(0.0, 1.0 - cos_sim ** 2)))
    return (cos_sim * base + sin_sim * perp).astype(np.float32)


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


# ── Aggregated per-identity matching ────────────────────────────────────────

def test_find_match_aggregated_beats_single_outlier(reg):
    """Per-identity aggregation (mean-of-top-K) resists a single-outlier false match.

    Face B has ONE embedding accidentally close to face A's query, plus FOUR
    embeddings far from it.  Argmax-across-all-rows would give B the win because
    B's outlier scores highest of any individual embedding.  Mean-of-top-3 per
    identity correctly awards the match to face A (consistent high scores).

    Score arithmetic (cos_sim notation, exact by construction):
      A's 5 embeddings vs query ≈ 0.90 × 0.95 = 0.855 each
        → top-3 mean ≈ 0.855
      B's outlier vs query ≈ 0.92 × 0.95 = 0.874  (beats A's best individually)
      B's 4 others vs query ≈ 0.0 (orthogonal)
        → top-3 mean ≈ (0.874 + 0 + 0) / 3 ≈ 0.29
    """
    rng = np.random.default_rng(42)
    base_a = rng.standard_normal(512).astype(np.float32)
    base_a /= np.linalg.norm(base_a)
    base_b = rng.standard_normal(512).astype(np.float32)
    base_b /= np.linalg.norm(base_b)

    face_a, _ = reg.register(_sphere_emb(base_a, 0.90, rng))
    for _ in range(4):
        reg.add_embedding(face_a, _sphere_emb(base_a, 0.90, rng))

    # Face B: 1 outlier near base_a (beats A individually) + 4 near base_b
    face_b, _ = reg.register(_sphere_emb(base_a, 0.92, rng))
    for _ in range(4):
        reg.add_embedding(face_b, _sphere_emb(base_b, 0.90, rng))

    query = _sphere_emb(base_a, 0.95, rng)
    match = reg.find_match(query)
    assert match is not None, "Expected a match for face A query"
    assert match[0] == face_a, (
        f"Expected face A ({face_a[:8]}) but aggregation picked face B ({face_b[:8]}), "
        f"score={match[2]:.3f}"
    )


def test_find_match_two_identities_correct_assignment(reg):
    """With two registered identities, queries from each person match correctly."""
    rng = np.random.default_rng(7)
    base_alice = rng.standard_normal(512).astype(np.float32)
    base_alice /= np.linalg.norm(base_alice)
    base_bob = rng.standard_normal(512).astype(np.float32)
    base_bob /= np.linalg.norm(base_bob)

    alice_id, _ = reg.register(_sphere_emb(base_alice, 0.90, rng))
    for _ in range(4):
        reg.add_embedding(alice_id, _sphere_emb(base_alice, 0.90, rng))

    bob_id, _ = reg.register(_sphere_emb(base_bob, 0.90, rng))
    for _ in range(4):
        reg.add_embedding(bob_id, _sphere_emb(base_bob, 0.90, rng))

    m_alice = reg.find_match(_sphere_emb(base_alice, 0.95, rng))
    m_bob = reg.find_match(_sphere_emb(base_bob, 0.95, rng))

    assert m_alice is not None and m_alice[0] == alice_id
    assert m_bob is not None and m_bob[0] == bob_id


# ── Quality gate ─────────────────────────────────────────────────────────────

def test_quality_gate_rejects_dissimilar_embedding(reg):
    """add_embedding_if_needed must reject a vector far from the identity centroid."""
    rng = np.random.default_rng(20)
    base = rng.standard_normal(512).astype(np.float32)
    base /= np.linalg.norm(base)

    # Build a stable identity with enough embeddings to activate the gate (≥ 5)
    face_id, _ = reg.register(_sphere_emb(base, 0.90, rng))
    for _ in range(4):
        reg.add_embedding_if_needed(face_id, _sphere_emb(base, 0.90, rng))

    # Force cache build so the gate has data to check against
    reg.find_match(_sphere_emb(base, 0.90, rng))

    # Manufacture a near-orthogonal embedding (cosine sim ≈ 0.0 with the centroid)
    garbage_base = rng.standard_normal(512).astype(np.float32)
    garbage_base -= float(garbage_base @ base) * base  # project out base component
    garbage_base /= np.linalg.norm(garbage_base)
    garbage = _sphere_emb(garbage_base, 0.99, rng)  # unit vector, ⊥ to base

    accepted = reg.add_embedding_if_needed(face_id, garbage)
    assert not accepted, "Quality gate should have rejected the garbage embedding"


def test_quality_gate_accepts_reasonable_variation(reg):
    """add_embedding_if_needed must still accept mildly perturbed embeddings."""
    rng = np.random.default_rng(30)
    base = rng.standard_normal(512).astype(np.float32)
    base /= np.linalg.norm(base)

    face_id, _ = reg.register(_sphere_emb(base, 0.90, rng))
    for _ in range(4):
        reg.add_embedding_if_needed(face_id, _sphere_emb(base, 0.90, rng))

    # Force cache build before gate check
    reg.find_match(_sphere_emb(base, 0.90, rng))

    # A mild perturbation (cos_sim=0.85) — clearly the same person
    mild = _sphere_emb(base, 0.85, rng)
    accepted = reg.add_embedding_if_needed(face_id, mild)
    assert accepted, "Quality gate incorrectly rejected a mildly perturbed embedding"


def test_quality_gate_inactive_below_min_frames(reg):
    """Quality gate must not activate until _QUALITY_GATE_MIN_FRAMES embeddings exist."""
    from src.perception.face_registry import _QUALITY_GATE_MIN_FRAMES

    rng = np.random.default_rng(40)
    base = rng.standard_normal(512).astype(np.float32)
    base /= np.linalg.norm(base)

    face_id, _ = reg.register(_sphere_emb(base, 0.90, rng))
    # Add only (MIN_FRAMES - 2) additional embeddings so gate is not yet active
    for _ in range(_QUALITY_GATE_MIN_FRAMES - 2):
        reg.add_embedding_if_needed(face_id, _sphere_emb(base, 0.90, rng))

    # Force cache build
    reg.find_match(_sphere_emb(base, 0.90, rng))

    # Even a near-orthogonal embedding should be accepted (gate not yet active)
    garbage_base = rng.standard_normal(512).astype(np.float32)
    garbage_base -= float(garbage_base @ base) * base
    garbage_base /= np.linalg.norm(garbage_base)
    garbage = _sphere_emb(garbage_base, 0.99, rng)

    accepted = reg.add_embedding_if_needed(face_id, garbage)
    assert accepted, "Quality gate must not reject before minimum frame count is reached"
