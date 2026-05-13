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
                "is_new": bool, "match_score": float}],
     "backend": "hailo"|"cpu"|"sim",
     "ts": float}

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
from dataclasses import dataclass
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

import numpy as _np
_ZERO_EMB = _np.zeros(512, dtype=_np.float32)  # placeholder when embedder is unavailable


@dataclass
class PerceptionConfig:
    max_fps: float = 10.0          # detection FPS — Hailo SCRFD runs ≫10 fps; CPU Haar caps itself naturally
    conf_threshold: float = 0.65   # raised from 0.45 to cut false positives on real frames
    nms_threshold: float = 0.4     # NMS IoU threshold
    recognition_enabled: bool = True  # enable ArcFace identity recognition


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
        self._cache_ttl: float = 10.0    # 10s — bridges brief detection gaps only, not person changes
        self._cache_dist: float = 160.0 # pixel radius to consider "same face" (wider tolerance)
        # Tight TTL used to skip re-embedding entirely when the same face was
        # just identified at the same location. Short enough that a person
        # swap can't slip through, long enough to cover several detection ticks.
        self._reuse_ttl: float = 1.0
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
                    self._registry = FaceRegistry()
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
        log.debug("PerceptionService: pos_cache purged on bulk face delete")

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
                }

                # Identity recognition — only when landmarks are available for alignment
                if self._embedder and self._registry and f.landmarks and len(f.landmarks) >= 5:
                    # Fast path: same face at same position within reuse_ttl —
                    # skip embedding + registry match entirely.
                    fresh = self._find_cached_face(
                        f.centroid[0], f.centroid[1], max_age=self._reuse_ttl,
                    )
                    if fresh:
                        face_id, name = fresh
                        self._registry.update_seen(face_id)
                        self._update_pos_cache(face_id, name, f.centroid[0], f.centroid[1])
                        entry["face_id"] = face_id
                        entry["name"] = name
                        entry["is_new"] = False
                        entry["match_score"] = 0.0
                        face_list.append(entry)
                        continue
                    try:
                        emb = self._embedder.embed(frame, f.landmarks)
                        embedder_ok = self._embedder.hardware_ready and emb.any()

                        if embedder_ok:
                            # ── Hardware ArcFace path ──────────────────────────
                            match = self._registry.find_match(emb)
                            if match:
                                face_id, name, score = match
                                self._registry.update_seen(face_id)
                                self._registry.add_embedding_if_needed(face_id, emb)
                                entry["face_id"] = face_id
                                entry["name"] = name
                                entry["is_new"] = False
                                entry["match_score"] = round(score, 3)
                                self._update_pos_cache(face_id, name, f.centroid[0], f.centroid[1])
                            else:
                                cached = self._find_cached_face(f.centroid[0], f.centroid[1])
                                if cached:
                                    face_id, name = cached
                                    self._registry.update_seen(face_id)
                                    self._registry.add_embedding_if_needed(face_id, emb)
                                    entry["face_id"] = face_id
                                    entry["name"] = name
                                    entry["is_new"] = False
                                    entry["match_score"] = 0.0
                                else:
                                    face_id, name = self._identify_or_register(
                                        frame, f, emb
                                    )
                                    entry["face_id"] = face_id
                                    entry["name"] = name
                                    entry["is_new"] = (name.startswith("Guest "))
                                    entry["match_score"] = 0.0
                        else:
                            # ── Sim mode (ArcFace unavailable) — position-cache + crop match ──
                            cached = self._find_cached_face(f.centroid[0], f.centroid[1])
                            if cached:
                                face_id, name = cached
                                self._registry.update_seen(face_id)
                                entry["face_id"] = face_id
                                entry["name"] = name
                                entry["is_new"] = False
                                entry["match_score"] = 0.0
                            else:
                                face_id, name = self._identify_or_register(
                                    frame, f, emb
                                )
                                entry["face_id"] = face_id
                                entry["name"] = name
                                entry["is_new"] = (name.startswith("Guest "))
                                entry["match_score"] = 0.0
                    except Exception:
                        log.exception("face recognition failed for one face")

                face_list.append(entry)

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

    def _find_cached_face(self, cx: float, cy: float, max_age: Optional[float] = None):
        """Return (face_id, name) from position cache if a nearby face was seen recently.

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
            return (best["face_id"], best["name"]) if best else None

    def _update_pos_cache(self, face_id: str, name: str, cx: float, cy: float) -> None:
        """Add or refresh a face entry in the position cache."""
        now = time.monotonic()
        with self._pos_cache_lock:
            for entry in self._pos_cache:
                if entry["face_id"] == face_id:
                    entry.update({"cx": cx, "cy": cy, "name": name, "ts": now})
                    return
            self._pos_cache.append({"face_id": face_id, "name": name, "cx": cx, "cy": cy, "ts": now})

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
                if emb is not None and emb.any():
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
