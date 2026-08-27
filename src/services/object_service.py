"""
Object detection service — runs YOLOv8s on live camera frames.

Subscribes to ``vision.frame_ready``, pulls the latest camera frame from
VisionService, runs :class:`~src.perception.object_detector.ObjectDetector`,
and publishes results on the bus.

Also handles ``vision.describe`` — builds a natural-language description of
everything currently seen (faces + objects) and speaks it via ``av.say``.
It also handles ``vision.object_query`` so voice/CLI callers can ask for an
object by a free-form phrase and get the closest detected match.

Topics published
----------------
perception.objects
    {"objects": [{"label": str, "class_id": int, "confidence": float,
                  "bbox": [x1, y1, x2, y2]}],
     "count": int,
     "backend": "hailo"|"sim",
     "frame_w": int, "frame_h": int,
     "ts": float}

object.enabled_changed
    {"enabled": bool}

vision.object_query_result
    {"ok": bool, "query": str, "terms": [...], "results": [...], "message": str}

Topics subscribed
-----------------
vision.frame_ready       — triggers detection on the latest frame
vision.describe          — triggers spoken scene description
object.set_enabled       — ``{"enabled": bool}`` toggle detection at runtime
vision.object_query      — ``{"query": str, "speak": bool}`` promptable object search
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.perception.object_vocabulary import match_query_to_objects

log = logging.getLogger(__name__)

_STATE_DIR              = Path.home() / ".config" / "desktop-assistant"
_OBJ_DETECT_STATE_FILE  = _STATE_DIR / "object_detection_enabled.txt"


@dataclass
class ObjectConfig:
    max_fps: float = 2.0        # object detection FPS (separate from face detection)
    conf_threshold: float = 0.40
    min_box_area: int = 400      # discard tiny noisy boxes
    max_objects: int = 8        # cap on detections sent to the overlay per frame
    enabled: bool = True        # whether object detection runs at startup
    temporal_confirmations: int = 2
    iou_threshold: float = 0.50
    open_vocab_threshold: float = 0.55
    hold_seconds: float = 2.0   # keep a missed detection visible this long before dropping it


class ObjectService(Service):
    name = "object"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        detector=None,
        config: Optional[ObjectConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._detector = detector
        self._cfg = config or ObjectConfig()
        self._min_interval = 1.0 / max(self._cfg.max_fps, 0.1)
        self._last_detect_ts: float = 0.0
        self._enabled: bool = self._cfg.enabled
        self._unsubs: list = []
        self._frame_queue: queue.Queue = queue.Queue(maxsize=1)
        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._recent_detections: deque = deque(maxlen=max(2, self._cfg.temporal_confirmations))
        # Sticky "held" detections — carries a detection forward across frames
        # where the detector momentarily misses it, so overlay boxes/labels
        # don't flicker in and out. Keyed by an opaque id; each entry tracks
        # the last-seen detection plus a monotonic timestamp.
        self._held: dict = {}
        self._held_seq: int = 0

    @property
    def detection_enabled(self) -> bool:
        return self._enabled

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_start(self) -> None:
        # Restore persisted enabled state (overrides ObjectConfig default).
        if _OBJ_DETECT_STATE_FILE.exists():
            try:
                saved = _OBJ_DETECT_STATE_FILE.read_text().strip().lower()
                self._enabled = saved != "false"
                log.info("ObjectService: restored detection_enabled=%s", self._enabled)
            except Exception as exc:
                log.warning("ObjectService: failed to restore enabled state: %s", exc)

        if self._detector is None:
            from src.perception.object_detector import ObjectDetector
            self._detector = ObjectDetector(
                conf_threshold=self._cfg.conf_threshold,
                min_box_area=self._cfg.min_box_area,
                iou_threshold=self._cfg.iou_threshold,
            )

        self._stop_evt.clear()
        self._worker = threading.Thread(
            target=self._detection_loop, daemon=True, name="object-worker"
        )
        self._worker.start()

        self._unsubs.append(
            self.bus.subscribe("vision.frame_ready", self._on_frame_ready)
        )
        self._unsubs.append(
            self.bus.subscribe("vision.describe", self._on_describe)
        )
        self._unsubs.append(
            self.bus.subscribe("object.set_enabled", self._on_set_enabled)
        )
        self._unsubs.append(
            self.bus.subscribe("vision.object_query", self._on_object_query)
        )
        log.info(
            "ObjectService started — backend=%s  max_fps=%.1f  enabled=%s",
            self._detector.backend,
            self._cfg.max_fps,
            self._enabled,
        )

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
        log.info("ObjectService stopped")

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_frame_ready(self, _topic, _payload) -> None:
        if not self._enabled:
            return
        if self._vision_svc is not None and not self._vision_svc.hardware_ready:
            return
        # Pre-check: skip waking the worker if we know it's too soon.
        # Reading _last_detect_ts from another thread is safe in CPython (GIL).
        if time.monotonic() - self._last_detect_ts < self._min_interval * 0.9:
            return
        try:
            self._frame_queue.put_nowait(True)
        except queue.Full:
            pass

    def _on_set_enabled(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "enabled" not in payload:
            return
        self._enabled = bool(payload["enabled"])
        log.info("ObjectService detection enabled=%s", self._enabled)
        if not self._enabled:
            # Drain any queued frames so the worker won't process stale work.
            while not self._frame_queue.empty():
                try:
                    self._frame_queue.get_nowait()
                except Exception:
                    break
            # Clear held/recent state so stale boxes don't reappear on re-enable.
            self._recent_detections.clear()
            self._held.clear()
        # Persist so the setting survives daemon restarts.
        try:
            _STATE_DIR.mkdir(parents=True, exist_ok=True)
            _OBJ_DETECT_STATE_FILE.write_text("true" if self._enabled else "false")
        except Exception as exc:
            log.warning("ObjectService: failed to persist enabled state: %s", exc)
        if self.bus is not None:
            self.bus.publish("object.enabled_changed", {"enabled": self._enabled})

    def _on_describe(self, _topic, _payload) -> None:
        """Build a natural-language scene description and speak it."""
        if self.bus is None:
            return
        faces_payload = self.bus.last("perception.faces")
        objs_payload  = self.bus.last("perception.objects")
        text = _build_scene_description(faces_payload, objs_payload)
        self.bus.publish("av.say", {"text": text})

    def _on_object_query(self, _topic, payload) -> None:
        if self.bus is None:
            return
        query = ""
        speak = True
        if isinstance(payload, dict):
            query = str(payload.get("query", "")).strip()
            speak = bool(payload.get("speak", True))
        result = self.query_objects(query, speak=speak)
        self.bus.publish("vision.object_query_result", result)

    # ── Detection worker ───────────────────────────────────────────────

    def _detection_loop(self) -> None:
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

            # Guard: enabled flag may have been cleared while this frame was queued.
            if not self._enabled:
                continue

            try:
                detections = self._detector.detect(frame)
            except Exception:
                log.exception("object detection failed")
                continue

            # If any faces are currently detected, suppress "person" labels to
            # avoid double-labelling the same subject.
            if detections and self.bus is not None:
                faces_payload = self.bus.last("perception.faces")
                if faces_payload and faces_payload.get("faces"):
                    detections = [d for d in detections if d.label != "person"]

            # Cap to max_objects (detections are already sorted by confidence).
            if len(detections) > self._cfg.max_objects:
                detections = detections[:self._cfg.max_objects]

            stable_detections = self._apply_temporal_filter(detections)
            held_detections = self._apply_hold(stable_detections, now)
            src_h, src_w = frame.shape[:2]
            self.bus.publish(
                "perception.objects",
                {
                    "objects": [
                        {
                            "label":      d.label,
                            "class_id":   d.class_id,
                            "confidence": d.confidence,
                            "bbox":       d.bbox,
                        }
                        for d in held_detections
                    ],
                    "count":    len(held_detections),
                    "backend":  self._detector.backend,
                    "frame_w":  src_w,
                    "frame_h":  src_h,
                    "ts":       time.time(),
                },
            )

    # ── Internal ───────────────────────────────────────────────────────

    @staticmethod
    def _iou(box_a, box_b) -> float:
        x1 = max(box_a[0], box_b[0])
        y1 = max(box_a[1], box_b[1])
        x2 = min(box_a[2], box_b[2])
        y2 = min(box_a[3], box_b[3])
        inter_w = max(0.0, x2 - x1)
        inter_h = max(0.0, y2 - y1)
        inter = inter_w * inter_h
        area_a = max(0.0, (box_a[2] - box_a[0]) * (box_a[3] - box_a[1]))
        area_b = max(0.0, (box_b[2] - box_b[0]) * (box_b[3] - box_b[1]))
        union = area_a + area_b - inter
        if union <= 0.0:
            return 0.0
        return inter / union

    def _apply_temporal_filter(self, detections):
        if not detections:
            self._recent_detections.clear()
            return []

        prev = list(self._recent_detections)
        kept = []
        for det in detections:
            if not prev:
                kept.append(det)
                continue
            if any(
                prev_det[0] == det.label and self._iou(prev_det[1], det.bbox) >= self._cfg.iou_threshold
                for prev_det in prev
            ):
                kept.append(det)

        self._recent_detections = deque(
            [(det.label, det.bbox) for det in detections],
            maxlen=max(2, self._cfg.temporal_confirmations),
        )
        return kept

    def _apply_hold(self, confirmed_detections, now: float):
        """
        Smooth detections over time so a briefly-missed object doesn't
        instantly disappear from the overlay/bus. A detection that matches
        (same label + overlapping box) an already-held entry refreshes it;
        an unmatched held entry is kept — using its last-known box/score —
        until ``hold_seconds`` elapses since it was last actually seen, then
        it's dropped. New detections become new held entries immediately.
        """
        matched_ids: set = set()
        for det in confirmed_detections:
            match_id = None
            for hid, entry in self._held.items():
                if hid in matched_ids:
                    continue
                if entry["det"].label == det.label and self._iou(entry["det"].bbox, det.bbox) >= self._cfg.iou_threshold:
                    match_id = hid
                    break
            if match_id is None:
                self._held_seq += 1
                match_id = self._held_seq
            self._held[match_id] = {"det": det, "last_seen": now}
            matched_ids.add(match_id)

        # Drop held entries that have aged past the hold window without a
        # fresh match this frame.
        expired = [
            hid for hid, entry in self._held.items()
            if hid not in matched_ids and now - entry["last_seen"] > self._cfg.hold_seconds
        ]
        for hid in expired:
            del self._held[hid]

        held_detections = [entry["det"] for entry in self._held.values()]
        held_detections.sort(key=lambda d: d.confidence, reverse=True)
        return held_detections[:self._cfg.max_objects]

    def query_objects(self, query: str, *, speak: bool = True) -> dict:
        """Resolve *query* against the latest detected objects."""
        objs_payload = self.bus.last("perception.objects") if self.bus is not None else None
        objects = []
        if isinstance(objs_payload, dict):
            objects = list(objs_payload.get("objects", []) or [])
        result = match_query_to_objects(query, objects, threshold=self._cfg.open_vocab_threshold)
        if not result["ok"] and not result.get("message"):
            result["message"] = f"I don't see a good match for {query!r}."
        if speak and self.bus is not None and result.get("message"):
            self.bus.publish("av.say", {"text": result["message"]})
        return result

    def _get_frame(self):
        if self._vision_svc is not None:
            try:
                return self._vision_svc.latest_frame()
            except Exception:
                log.warning("ObjectService: could not get frame from VisionService")
        return None


# ── Scene description builder ──────────────────────────────────────────────

def _build_scene_description(
    faces_payload,
    objs_payload,
) -> str:
    """Return a natural-language sentence describing the current scene."""
    face_names: list[str] = []
    if isinstance(faces_payload, dict):
        for f in faces_payload.get("faces", []):
            name = f.get("name")
            if name and not name.startswith("Guest"):
                face_names.append(name)

    obj_labels: list[str] = []
    if isinstance(objs_payload, dict):
        for obj in objs_payload.get("objects", []):
            label = obj.get("label", "")
            if label:
                obj_labels.append(label)

    if not face_names and not obj_labels:
        return "I don't see anything in particular right now."

    parts: list[str] = []

    if face_names:
        parts.append(_join_names(face_names))

    if obj_labels:
        counts = Counter(obj_labels)
        obj_parts = []
        for label, count in counts.most_common():
            if count == 1:
                article = "an" if label[0].lower() in "aeiou" else "a"
                obj_parts.append(f"{article} {label}")
            else:
                obj_parts.append(f"{count} {label}s")
        parts.append(_join_names(obj_parts))

    if len(parts) == 1:
        return f"I see {parts[0]}."
    return f"I see {parts[0]}, and {parts[1]}."


def _join_names(names: list[str]) -> str:
    if len(names) == 0:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"
