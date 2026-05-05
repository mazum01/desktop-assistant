"""
Perception service — face detection on live camera frames.

Subscribes to ``vision.frame_ready``, pulls the latest frame from
VisionService, runs FaceDetector, and publishes results on the bus.

Topics published
----------------
perception.faces
    {"count": int,
     "faces": [{"bbox": [x1,y1,x2,y2], "centroid": [cx,cy],
                "confidence": float, "landmarks": [[x,y],…] | null}],
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
import time
from dataclasses import dataclass
from typing import Optional

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


@dataclass
class PerceptionConfig:
    max_fps: float = 10.0          # detection frequency cap
    conf_threshold: float = 0.45   # face confidence threshold
    nms_threshold: float = 0.4     # NMS IoU threshold


class PerceptionService(Service):
    name = "perception"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        detector=None,
        config: Optional[PerceptionConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._detector = detector
        self._cfg = config or PerceptionConfig()
        self._min_interval = 1.0 / max(self._cfg.max_fps, 0.1)
        self._last_detect_ts: float = 0.0
        self._unsubs: list = []

    # ── Lifecycle ─────────────────────────────────────────────────────

    def on_start(self) -> None:
        if self._detector is None:
            from src.perception.face_detector import FaceDetector
            self._detector = FaceDetector(
                conf_threshold=self._cfg.conf_threshold,
                nms_threshold=self._cfg.nms_threshold,
            )
        self._unsubs.append(
            self.bus.subscribe("vision.frame_ready", self._on_frame_ready)
        )
        log.info(
            "PerceptionService started — backend=%s  max_fps=%.1f",
            self._detector.backend,
            self._cfg.max_fps,
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
        if self._detector is not None:
            try:
                self._detector.close()
            except Exception:
                pass
        log.info("PerceptionService stopped")

    # ── Bus handler ───────────────────────────────────────────────────

    def _on_frame_ready(self, _topic, _payload) -> None:
        now = time.monotonic()
        if now - self._last_detect_ts < self._min_interval:
            return
        self._last_detect_ts = now

        # Pull latest frame from VisionService (in-process, zero-copy path)
        frame = self._get_frame()
        if frame is None:
            return

        try:
            faces = self._detector.detect(frame)
        except Exception:
            log.exception("face detection failed")
            self.bus.publish("perception.error", {"reason": "detect_failed"})
            return

        face_list = [
            {
                "bbox": list(f.bbox),
                "centroid": list(f.centroid),
                "confidence": round(f.confidence, 3),
                "landmarks": [list(pt) for pt in f.landmarks] if f.landmarks else None,
            }
            for f in faces
        ]
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

    def _get_frame(self):
        if self._vision_svc is not None:
            try:
                return self._vision_svc.latest_frame()
            except Exception:
                log.warning("Could not get frame from VisionService")
        return None
