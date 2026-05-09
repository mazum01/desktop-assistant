"""
Object detection service — runs YOLOv8s on live camera frames.

Subscribes to ``vision.frame_ready``, pulls the latest camera frame from
VisionService, runs :class:`~src.perception.object_detector.ObjectDetector`,
and publishes results on the bus.

Also handles ``vision.describe`` — builds a natural-language description of
everything currently seen (faces + objects) and speaks it via ``av.say``.

Topics published
----------------
perception.objects
    {"objects": [{"label": str, "class_id": int, "confidence": float,
                  "bbox": [x1, y1, x2, y2]}],
     "count": int,
     "backend": "hailo"|"sim",
     "frame_w": int, "frame_h": int,
     "ts": float}

Topics subscribed
-----------------
vision.frame_ready   — triggers detection on the latest frame
vision.describe      — triggers spoken scene description
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

log = logging.getLogger(__name__)


@dataclass
class ObjectConfig:
    max_fps: float = 3.0    # object detection FPS (separate from face detection)
    conf_threshold: float = 0.40


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
        self._unsubs: list = []
        self._frame_queue: queue.Queue = queue.Queue(maxsize=1)
        self._worker: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_start(self) -> None:
        if self._detector is None:
            from src.perception.object_detector import ObjectDetector
            self._detector = ObjectDetector(conf_threshold=self._cfg.conf_threshold)

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
        log.info(
            "ObjectService started — backend=%s  max_fps=%.1f",
            self._detector.backend,
            self._cfg.max_fps,
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
        if self._vision_svc is not None and not self._vision_svc.hardware_ready:
            return
        try:
            self._frame_queue.put_nowait(True)
        except queue.Full:
            pass

    def _on_describe(self, _topic, _payload) -> None:
        """Build a natural-language scene description and speak it."""
        if self.bus is None:
            return
        faces_payload = self.bus.last("perception.faces")
        objs_payload  = self.bus.last("perception.objects")
        text = _build_scene_description(faces_payload, objs_payload)
        self.bus.publish("av.say", {"text": text})

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

            try:
                detections = self._detector.detect(frame)
            except Exception:
                log.exception("object detection failed")
                continue

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
                        for d in detections
                    ],
                    "count":    len(detections),
                    "backend":  self._detector.backend,
                    "frame_w":  src_w,
                    "frame_h":  src_h,
                    "ts":       time.time(),
                },
            )

    # ── Internal ───────────────────────────────────────────────────────

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
