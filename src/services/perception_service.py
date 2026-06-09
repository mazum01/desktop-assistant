"""
Perception service — face detection + identity recognition on live camera frames.

Subscribes to ``vision.frame_ready``, pulls the latest frame from
VisionService, runs FaceDetector, extracts embeddings via FaceEmbedder,
looks up identities in FaceRegistry, and publishes results on the bus.

Topics published
----------------
perception.faces
    {"count": int,
     "faces": [{"bbox": [x1,y1,x2,y2], "centroid": [cx,cy],
                "confidence": float, "landmarks": [[x,y],…] | null,
                "face_id": str | null, "name": str | null,
                "is_new": bool, "match_score": float,
                "is_stabilizing": bool,
                "stabilization_changed": bool,
                "initial_name": str | null}],
     "backend": "hailo"|"cpu"|"sim",
     "ts": float}

Identity stabilisation
----------------------
When a face appears at a new position the service accumulates
``_STAB_WINDOW_FRAMES`` independent embedding matches before committing
to an identity.  During this window the pos-cache fast path is bypassed
so each frame gets a fresh ArcFace match.  Once committed:

* ``is_stabilizing`` is False.
* If the final majority identity differs from the first-frame guess,
  ``stabilization_changed=True`` and ``initial_name`` carries the
  original (wrong) label so callers can issue a contrite correction.

perception.error
    {"reason": str}

Topics subscribed
-----------------
vision.frame_ready  — triggers detection on the latest frame

Configuration
-------------
PerceptionConfig.max_fps controls how many detections per second are
attempted (default 10). Frames arriving faster than this are skipped.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.perception.depth_estimator import face_size_depth, focal_px_from_fov, to_3d

log = logging.getLogger(__name__)

import numpy as _np
_ZERO_EMB = _np.zeros(512, dtype=_np.float32)  # placeholder when embedder is unavailable

# ── Identity-stabilisation constants ─────────────────────────────────────────
# Before committing to an identity, accumulate this many independent ArcFace
# matches.  The pos-cache fast path is bypassed during this window so every
# frame gets a fresh embedding + registry look-up.
_STAB_WINDOW_FRAMES: int   = 8     # frames to accumulate before committing
_STAB_GRID_PX:       int   = 60    # spatial cell size for position keying (px)
_STAB_TTL_S:         float = 12.0  # abandon stab entry if no update for this long


@dataclass
class PerceptionConfig:
    max_fps: float = 10.0          # detection FPS — Hailo SCRFD runs ≫10 fps; CPU Haar caps itself naturally
    conf_threshold: float = 0.65   # raised from 0.45 to cut false positives on real frames
    nms_threshold: float = 0.4     # NMS IoU threshold
    recognition_enabled: bool = True  # enable ArcFace identity recognition
    match_threshold: float = 0.50  # cosine similarity threshold for identity matching
    min_face_px: int = 80          # skip embedding for faces narrower or shorter than this (pixels)
    # Depth estimation (face-size method — always-on when focal_px > 0)
    focal_px: float = 0.0          # 0 = derive from fov_degrees + frame_width at runtime
    fov_degrees: float = 100.0     # horizontal FOV of the primary camera
    frame_width: int = 640         # primary camera frame width (for focal derivation)
    known_face_width_m: float = 0.145
    min_depth_m: float = 0.25
    max_depth_m: float = 6.0
    # How long an unrecognized face must be continuously visible before being
    # registered in the DB and shown in the UI (seconds; 0 = register immediately)
    guest_intro_delay_s: float = 120.0


class PerceptionService(Service):
    name = "perception"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        detector=None,
        embedder=None,
        registry=None,
        config: Optional[PerceptionConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._detector = detector
        self._embedder = embedder
        self._registry = registry
        self._cfg = config or PerceptionConfig()
        self._min_interval = 1.0 / max(self._cfg.max_fps, 0.1)
        self._last_detect_ts: float = 0.0
        self._unsubs: list = []
        self._pos_cache: list = []
        self._pos_cache_lock = threading.Lock()
        self._cache_ttl: float = 10.0
        self._cache_dist: float = 160.0
        self._reuse_ttl: float = 1.0
        # Stabilisation buffer: pos_key → {name_votes, id_map, frames, done, initial_name, last_seen}
        self._stab_buffer: dict = {}
        # Pending guest buffer: tracks unknown faces silently until the registration
        # delay elapses.  Key: pos_key of first sighting; value: metadata dict.
        self._pending_guests: dict[str, dict] = {}
        # Compute focal length once (lazy — resolved at first detection)
        self._focal_px: Optional[float] = self._cfg.focal_px if self._cfg.focal_px > 0 else None
        # Detection runs in its own thread so it never blocks the VisionService tick.
        self._frame_queue: queue.Queue = queue.Queue(maxsize=1)
        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    # ── Lifecycle ─────────────────────────────────────────────────────

    def on_start(self) -> None:
        if self._detector is None:
            from src.perception.face_detector import FaceDetector
            self._detector = FaceDetector(
                conf_threshold=self._cfg.conf_threshold,
                nms_threshold=self._cfg.nms_threshold,
            )

        if self._cfg.recognition_enabled:
            if self._embedder is None:
                try:
                    from src.perception.face_embedder import FaceEmbedder
                    self._embedder = FaceEmbedder()
                except Exception as exc:
                    log.warning("FaceEmbedder init failed (%s) — recognition disabled", exc)
            if self._registry is None:
                try:
                    from src.perception.face_registry import FaceRegistry
                    self._registry = FaceRegistry(
                        match_threshold=self._cfg.match_threshold,
                    )
                except Exception as exc:
                    log.warning("FaceRegistry init failed (%s) — recognition disabled", exc)

        self._stop_evt.clear()
        self._worker = threading.Thread(
            target=self._detection_loop, daemon=True, name="perception-worker"
        )
        self._worker.start()

        self._unsubs.append(
            self.bus.subscribe("vision.frame_ready", self._on_frame_ready)
        )
        self._unsubs.append(
            self.bus.subscribe("face.deleted", self._on_face_deleted)
        )
        self._unsubs.append(
            self.bus.subscribe("face.guests_cleared", self._on_faces_cleared)
        )
        self._unsubs.append(
            self.bus.subscribe("face.registry_cleared", self._on_faces_cleared)
        )
        self._unsubs.append(
            self.bus.subscribe("face.refresh", self._on_face_refresh)
        )
        log.info(
            "PerceptionService started — backend=%s  max_fps=%.1f  recognition=%s",
            self._detector.backend,
            self._cfg.max_fps,
            "enabled" if (self._embedder and self._registry) else "disabled",
        )

    @property
    def hardware_ready(self) -> bool:
        return self._detector is not None and self._detector.backend == "hailo"

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self._stop_evt.set()
        if self._worker is not None:
            self._worker.join(timeout=5.0)
            self._worker = None
        if self._detector is not None:
            try:
                self._detector.close()
            except Exception:
                pass
        if self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                pass
        if self._registry is not None:
            try:
                self._registry.close()
            except Exception:
                pass
        log.info("PerceptionService stopped")

    # ── Bus handler (non-blocking — just signals the worker) ──────────

    def _on_frame_ready(self, _topic, _payload) -> None:
        # Skip detection on sim frames to avoid false positives from placeholder graphics.
        if self._vision_svc is not None and not self._vision_svc.hardware_ready:
            return
        # Non-blocking put; drop the signal if the worker is still busy with the previous frame.
        try:
            self._frame_queue.put_nowait(True)
        except queue.Full:
            pass  # worker still processing — skip this frame

    def _on_face_deleted(self, _topic, payload) -> None:
        """Purge a single deleted face from the position cache."""
        if not isinstance(payload, dict):
            return
        face_id = payload.get("face_id")
        if not face_id:
            return
        with self._pos_cache_lock:
            self._pos_cache = [e for e in self._pos_cache if e["face_id"] != face_id]
        log.debug("PerceptionService: purged face_id %s from pos_cache", face_id[:8])

    def _on_faces_cleared(self, _topic, payload) -> None:
        """Purge deleted faces from the position cache after a bulk delete."""
        if isinstance(payload, dict) and "face_ids" in payload:
            ids = set(payload["face_ids"])
            with self._pos_cache_lock:
                self._pos_cache = [e for e in self._pos_cache if e["face_id"] not in ids]
        else:
            with self._pos_cache_lock:
                self._pos_cache.clear()
            self._stab_buffer.clear()
            self._pending_guests.clear()
        log.debug("PerceptionService: pos_cache purged on bulk face delete")

    def _on_face_refresh(self, _topic, _payload) -> None:
        """Clear the position cache and reload embedding DB so Re-identify takes effect immediately.

        Without this handler, the Re-identify button publishes face.refresh but
        the position cache keeps serving stale (potentially wrong) identity
        assignments on every subsequent frame, making Re-identify ineffective.
        """
        with self._pos_cache_lock:
            self._pos_cache.clear()
        self._stab_buffer.clear()
        self._pending_guests.clear()
        if self._registry is not None:
            self._registry.reload()
        log.info("PerceptionService: pos_cache + stab_buffer cleared and embedding cache reloaded on face.refresh")

    # ── Detection worker (runs in its own thread) ──────────────────────

    def _detection_loop(self) -> None:
        """Consume frame signals from the queue and run detection + recognition."""
        while not self._stop_evt.is_set():
            try:
                self._frame_queue.get(timeout=0.5)
            except queue.Empty:
                continue

            now = time.monotonic()
            if now - self._last_detect_ts < self._min_interval:
                continue
            self._last_detect_ts = now

            frame = self._get_frame()
            if frame is None:
                continue

            try:
                faces = self._detector.detect(frame)
            except Exception:
                log.exception("face detection failed")
                self.bus.publish("perception.error", {"reason": "detect_failed"})
                continue

            face_list = []

            # Expire stale pending-guest entries (not seen in > _STAB_TTL_S seconds)
            _now_pg = time.monotonic()
            for _pk in [k for k, v in self._pending_guests.items()
                        if _now_pg - v["last_seen"] > _STAB_TTL_S]:
                log.debug("Pending guest %s expired (not seen for %.0fs)", _pk, _STAB_TTL_S)
                del self._pending_guests[_pk]

            for f in faces:
                entry = {
                    "bbox": list(f.bbox),
                    "centroid": list(f.centroid),
                    "confidence": round(f.confidence, 3),
                    "landmarks": [list(pt) for pt in f.landmarks] if f.landmarks else None,
                    "face_id": None,
                    "name": None,
                    "is_new": False,
                    "match_score": 0.0,
                    "is_stabilizing": False,
                    "stabilization_changed": False,
                    "initial_name": None,
                }

                # Identity recognition — only when landmarks are available for alignment
                if self._embedder and self._registry and f.landmarks and len(f.landmarks) >= 5:
                    # Skip embedding for faces that are too small — tiny/distant faces
                    # produce noisy embeddings that hurt matching quality.
                    x1, y1, x2, y2 = f.bbox
                    if (x2 - x1) < self._cfg.min_face_px or (y2 - y1) < self._cfg.min_face_px:
                        face_list.append(entry)
                        continue

                    cx, cy = f.centroid[0], f.centroid[1]
                    pos_key = self._pos_key(cx, cy)
                    now_stab = time.monotonic()

                    # Retrieve (and TTL-expire) the stabilisation entry for this position.
                    stab = self._stab_buffer.get(pos_key)
                    if stab and (now_stab - stab["last_seen"]) > _STAB_TTL_S:
                        del self._stab_buffer[pos_key]
                        stab = None
                    elif stab:
                        stab["last_seen"] = now_stab  # keep entry alive while face is present

                    # ── Pos-cache fast path (only when NOT actively stabilising) ──
                    # During the stabilisation window every frame must produce a
                    # fresh ArcFace embedding so votes are independent.
                    if stab is None or stab["done"]:
                        fresh = self._find_cached_face(cx, cy, max_age=self._reuse_ttl)
                        if fresh:
                            face_id, name, cached_score = fresh
                            self._registry.update_seen(face_id)
                            self._update_pos_cache(face_id, name, cx, cy, cached_score)
                            entry["face_id"] = face_id
                            entry["name"] = name
                            entry["is_new"] = False
                            entry["match_score"] = cached_score
                            face_list.append(entry)
                            continue

                    # ── Full embed + match (always during stabilisation) ───────
                    try:
                        emb = self._embedder.embed(frame, f.landmarks)
                        embedder_ok = self._embedder.hardware_ready and emb.any()
                        score = 0.0

                        if embedder_ok:
                            match = self._registry.find_match(emb)
                            if match:
                                face_id, name, score = match
                                self._registry.update_seen(face_id)
                                # Defer add_embedding_if_needed until identity is confirmed
                                if stab is None or stab["done"]:
                                    # Only commit to gallery if the crop was sharp
                                    if self._embedder.last_was_sharp_enough_to_store:
                                        self._registry.add_embedding_if_needed(face_id, emb)
                            else:
                                # Try tentative match before falling back to Guest registration.
                                # A tentative match (score in [0.45, threshold)) means we have
                                # a plausible candidate but not enough confidence to commit.
                                # Continue accumulating stabilisation votes without creating
                                # a new Guest entry.
                                tentative = self._registry.find_tentative_match(emb)
                                if tentative:
                                    face_id, name, score = tentative
                                    self._registry.update_seen(face_id)
                                    log.debug(
                                        "Tentative match %s → %r (score=%.3f)", face_id[:8], name, score
                                    )
                                else:
                                    cached = self._find_cached_face(cx, cy)
                                    if cached:
                                        face_id, name, score = cached
                                        self._registry.update_seen(face_id)
                                    else:
                                        result = self._check_pending_or_register(
                                            pos_key, frame, f, emb, cx, cy
                                        )
                                        if result is None:
                                            continue  # still in delay window; skip
                                        face_id, name = result
                        else:
                            # ── Sim mode / blurry frame — position-cache only ──────
                            # Do NOT create a new Guest from a blurry/zero embedding.
                            # Wait for a sharp frame before anchoring the identity.
                            cached = self._find_cached_face(cx, cy)
                            if cached:
                                face_id, name, score = cached
                                self._registry.update_seen(face_id)
                            else:
                                # Skip this detection — no reliable embedding available.
                                continue

                        # ── Stabilisation buffer update ────────────────────────
                        is_stabilizing = False
                        stabilization_changed = False
                        initial_name_out = None

                        if stab is None:
                            # Brand-new position — start stabilisation window
                            self._stab_buffer[pos_key] = {
                                "name_votes": Counter({name: 1}),
                                "id_map":     {name: face_id},
                                "frames":     1,
                                "done":       False,
                                "initial_name": name,
                                "last_seen":  now_stab,
                            }
                            is_stabilizing = True
                        elif not stab["done"]:
                            stab["name_votes"][name] += 1
                            stab["id_map"][name] = face_id  # keep latest face_id per name
                            stab["frames"] += 1

                            if stab["frames"] >= _STAB_WINDOW_FRAMES:
                                # Commit to the majority-vote winner
                                committed_name, _ = stab["name_votes"].most_common(1)[0]
                                face_id = stab["id_map"][committed_name]
                                name    = committed_name
                                initial_name_out    = stab["initial_name"]
                                stabilization_changed = committed_name != initial_name_out
                                stab["done"] = True
                                # Now safe to reinforce the confirmed identity
                                # — but only if the crop was sharp enough for storage.
                                if (
                                    embedder_ok
                                    and emb.any()
                                    and self._embedder.last_was_sharp_enough_to_store
                                ):
                                    self._registry.add_embedding_if_needed(face_id, emb)
                                log.info(
                                    "Identity stabilised at %s → %r (initial=%r, votes=%s)",
                                    pos_key, name, initial_name_out,
                                    dict(stab["name_votes"]),
                                )
                            else:
                                is_stabilizing = True

                        self._update_pos_cache(face_id, name, cx, cy, score)
                        entry["face_id"]              = face_id
                        entry["name"]                 = name
                        entry["is_new"]               = name.startswith("Guest ") and not is_stabilizing
                        entry["match_score"]          = round(score, 3)
                        entry["is_stabilizing"]       = is_stabilizing
                        entry["stabilization_changed"] = stabilization_changed
                        entry["initial_name"]         = initial_name_out

                    except Exception:
                        log.exception("face recognition failed for one face")

                face_list.append(entry)

            # ── Face-size depth estimation ─────────────────────────────
            focal = self._get_focal_px(frame)
            if focal is not None:
                h, w = frame.shape[:2]
                for f_entry in face_list:
                    bbox = f_entry.get("bbox")
                    if not bbox:
                        continue
                    bbox_w = bbox[2] - bbox[0]
                    depth = face_size_depth(
                        bbox_w, focal,
                        face_width_m=self._cfg.known_face_width_m,
                    )
                    if depth is not None and self._cfg.min_depth_m <= depth <= self._cfg.max_depth_m:
                        cx, cy = f_entry["centroid"]
                        x_m, y_m, z_m = to_3d(cx, cy, depth, focal, w, h)
                        f_entry["depth_m"] = round(depth, 3)
                        f_entry["pos_3d"] = [round(x_m, 3), round(y_m, 3), round(z_m, 3)]
                    else:
                        f_entry["depth_m"] = None
                        f_entry["pos_3d"] = None

            self.bus.publish(
                "perception.faces",
                {
                    "count": len(face_list),
                    "faces": face_list,
                    "backend": self._detector.backend,
                    "ts": time.time(),
                },
            )

    # ── Internal ──────────────────────────────────────────────────────

    def _get_focal_px(self, frame) -> Optional[float]:
        """Return focal length in pixels, computing it once from config if needed."""
        if self._focal_px is None:
            try:
                h, w = frame.shape[:2]
                self._focal_px = focal_px_from_fov(
                    self._cfg.frame_width or w,
                    self._cfg.fov_degrees,
                )
            except Exception:
                return None
        return self._focal_px

    def _pos_key(self, cx: float, cy: float) -> str:
        """Coarse grid key for the stabilisation buffer (``_STAB_GRID_PX``-pixel cells)."""
        return f"{int(cx / _STAB_GRID_PX)},{int(cy / _STAB_GRID_PX)}"

    def _find_cached_face(self, cx: float, cy: float, max_age: Optional[float] = None):
        """Return (face_id, name, match_score) from position cache if a nearby face was seen recently.

        ``max_age`` overrides the default ``_cache_ttl`` when provided — used for
        the embed-skip fast path which needs a much tighter time window.
        """
        now = time.monotonic()
        ttl = max_age if max_age is not None else self._cache_ttl
        with self._pos_cache_lock:
            self._pos_cache = [e for e in self._pos_cache if now - e["ts"] < self._cache_ttl]
            best = None
            best_dist = float("inf")
            for entry in self._pos_cache:
                if now - entry["ts"] > ttl:
                    continue
                dist = ((cx - entry["cx"]) ** 2 + (cy - entry["cy"]) ** 2) ** 0.5
                if dist < self._cache_dist and dist < best_dist:
                    best_dist = dist
                    best = entry
            if best is None:
                return None
            return (best["face_id"], best["name"], best.get("match_score", 0.0))

    def _update_pos_cache(self, face_id: str, name: str, cx: float, cy: float, match_score: float = 0.0) -> None:
        """Add or refresh a face entry in the position cache."""
        now = time.monotonic()
        with self._pos_cache_lock:
            for entry in self._pos_cache:
                if entry["face_id"] == face_id:
                    entry.update({"cx": cx, "cy": cy, "name": name, "match_score": match_score, "ts": now})
                    return
            self._pos_cache.append({"face_id": face_id, "name": name, "cx": cx, "cy": cy, "match_score": match_score, "ts": now})

    def _get_frame(self):
        if self._vision_svc is not None:
            try:
                return self._vision_svc.latest_frame()
            except Exception:
                log.warning("Could not get frame from VisionService")
        return None

    def _extract_crop(self, frame, bbox) -> Optional["np.ndarray"]:
        """Extract and return the face crop from *frame* given *bbox*."""
        try:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            crop = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
            return crop if crop.size > 0 else None
        except Exception:
            return None

    def _check_pending_or_register(
        self, pos_key: str, frame, detection, emb, cx: float, cy: float
    ) -> Optional[tuple]:
        """Gate new-face registration behind the configured guest_intro_delay.

        Tracks unrecognized faces in ``_pending_guests`` (keyed by pos_key) and
        returns None while the delay has not yet elapsed, causing the caller to
        skip publishing this face entirely.  Once the delay expires the face is
        promoted via ``_identify_or_register`` and its real (face_id, name) tuple
        is returned.

        If ``guest_intro_delay_s <= 0`` the check is bypassed and registration
        happens immediately (backward-compat / test mode).
        """
        delay_s = self._cfg.guest_intro_delay_s
        if delay_s <= 0:
            return self._identify_or_register(frame, detection, emb)

        now = time.monotonic()

        # Find an existing pending entry near this centroid
        pending_key: Optional[str] = None
        for pk, info in self._pending_guests.items():
            if (abs(info["cx"] - cx) <= _STAB_GRID_PX * 2
                    and abs(info["cy"] - cy) <= _STAB_GRID_PX * 2):
                pending_key = pk
                break

        if pending_key is None:
            # Brand-new unknown face — start the delay timer
            crop = self._extract_crop(frame, detection.bbox)
            self._pending_guests[pos_key] = {
                "first_seen": now,
                "last_seen": now,
                "best_emb": emb,
                "cx": cx,
                "cy": cy,
                "crop": crop,
            }
            log.debug(
                "Pending guest at %s — starting %.1f-min registration delay",
                pos_key, delay_s / 60.0,
            )
            return None

        info = self._pending_guests[pending_key]
        info["last_seen"] = now
        info["cx"] = cx
        info["cy"] = cy
        if (emb is not None and emb.any()
                and (info["best_emb"] is None or not info["best_emb"].any())):
            info["best_emb"] = emb

        elapsed = now - info["first_seen"]
        if elapsed < delay_s:
            return None

        # Delay elapsed — promote to real registration
        best_emb = info["best_emb"]
        if best_emb is None or not best_emb.any():
            best_emb = emb
        del self._pending_guests[pending_key]
        log.info(
            "Pending guest at %s promoted after %.0fs — registering now",
            pending_key, elapsed,
        )
        return self._identify_or_register(frame, detection, best_emb)

    def _identify_or_register(self, frame, detection, emb) -> tuple:
        """Identify a face when position cache and embedding matching both missed.

        Priority:
        1. Crop-based histogram similarity against stored thumbnails.
        2. Register as a new Guest — never assume an unrecognized face is a
           known person; a different person can walk in at any time.

        Returns (face_id, name).
        """
        crop = self._extract_crop(frame, detection.bbox)

        # 1. Crop similarity
        if crop is not None:
            crop_match = self._registry.find_match_by_crop(crop)
            if crop_match:
                face_id, name, score = crop_match
                self._registry.update_seen(face_id)
                if (
                    emb is not None
                    and emb.any()
                    and self._embedder is not None
                    and self._embedder.last_was_sharp_enough_to_store
                ):
                    self._registry.add_embedding_if_needed(face_id, emb)
                # Refresh thumbnail with a cleaner crop if needed
                if self._registry.thumbnail_path(face_id) is None:
                    self._registry.save_thumbnail(face_id, crop)
                self._update_pos_cache(face_id, name, detection.centroid[0], detection.centroid[1])
                log.debug("Crop-matched %s as %r (score=%.3f)", face_id[:8], name, score)
                return face_id, name

        # 2. Register as new Guest — never guess based on registry size alone
        face_id, auto_name = self._registry.register(emb if emb is not None else _ZERO_EMB)
        if crop is not None:
            self._registry.save_thumbnail(face_id, crop)
        self._update_pos_cache(face_id, auto_name, detection.centroid[0], detection.centroid[1])
        return face_id, auto_name

    # ── Training capture (called from WebService API) ──────────────────

    def capture_training_image(self, face_id: str) -> dict:
        """Grab the current camera frame, detect the most prominent face, and add
        an embedding + refresh the thumbnail for *face_id*.

        Returns a result dict:
            {"ok": True,  "embeddings_added": int, "thumbnail_updated": bool,
             "bbox": [x1,y1,x2,y2], "confidence": float}
            {"ok": False, "reason": str}
        """
        if self._registry is None:
            return {"ok": False, "reason": "registry_unavailable"}
        if self._detector is None:
            return {"ok": False, "reason": "detector_unavailable"}
        if self._registry.get_face(face_id) is None:
            return {"ok": False, "reason": "face_not_found"}

        frame = self._get_frame()
        if frame is None:
            return {"ok": False, "reason": "no_frame"}

        try:
            detections = self._detector.detect(frame)
        except Exception:
            log.exception("capture_training_image: detection failed")
            return {"ok": False, "reason": "detection_failed"}

        if not detections:
            return {"ok": False, "reason": "no_face_detected"}

        # Pick highest-confidence detection
        best = max(detections, key=lambda f: f.confidence)
        crop = self._extract_crop(frame, best.bbox)

        # Generate and store embedding when ArcFace is available
        added = 0
        bx1, by1, bx2, by2 = best.bbox
        face_large_enough = (bx2 - bx1) >= self._cfg.min_face_px and (by2 - by1) >= self._cfg.min_face_px
        if self._embedder and best.landmarks and len(best.landmarks) >= 5 and face_large_enough:
            try:
                emb = self._embedder.embed(frame, best.landmarks)
                if emb is not None and emb.any():
                    self._registry.add_embedding_if_needed(face_id, emb)
                    added = 1
            except Exception:
                log.exception("capture_training_image: embedding failed")

        # Always refresh thumbnail so the registry shows the latest image
        thumb_ok = False
        if crop is not None:
            thumb_ok = self._registry.save_thumbnail(face_id, crop)

        log.info(
            "capture_training_image: face=%s added=%d thumb=%s conf=%.2f",
            face_id[:8], added, thumb_ok, best.confidence,
        )
        return {
            "ok": True,
            "embeddings_added": added,
            "thumbnail_updated": thumb_ok,
            "bbox": [int(v) for v in best.bbox],
            "confidence": round(best.confidence, 3),
        }
