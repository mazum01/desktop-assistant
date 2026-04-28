"""
Vision service.

Owns a `Camera` instance, runs a continuous capture loop, exposes the
latest frame to in-process subscribers, and publishes lightweight
frame metadata on the bus so other services can react without us
spamming megabyte-sized payloads through the pub/sub layer.

Topics published:
    vision.frame_ready  {"index": int, "shape": (H, W, C), "ts": float}
    vision.error        {"reason": str}

Topics subscribed:
    vision.capture_still  {"path": str}     — write a JPEG still to *path*

Public accessor (in-process callers):
    svc.latest_frame() → np.ndarray | None
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class VisionService(Service):
    name = "vision"
    tick_seconds = 0.1   # 10 fps frame-publish cadence; the camera
                         # itself runs at its configured framerate

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        camera=None,
    ) -> None:
        super().__init__(bus=bus)
        self._camera = camera
        self._latest: Optional[np.ndarray] = None
        self._index = 0
        self._lock = threading.Lock()
        self._unsubs = []

    def on_start(self) -> None:
        if self._camera is None:
            from src.vision.camera import Camera, CameraConfig
            self._camera = Camera(CameraConfig())
        try:
            self._camera.start()
        except Exception:
            log.exception("camera.start() failed")
            self.bus.publish("vision.error", {"reason": "start_failed"})
            return

        self._unsubs.append(
            self.bus.subscribe("vision.capture_still", self._on_capture_still)
        )
        log.info(
            "VisionService started; hardware_ready=%s",
            getattr(self._camera, "hardware_ready", False),
        )

    def run_tick(self) -> None:
        if self._camera is None:
            return
        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("capture_frame failed")
            self.bus.publish("vision.error", {"reason": "capture_failed"})
            return
        with self._lock:
            self._latest = frame
            self._index += 1
            idx = self._index
        self.bus.publish(
            "vision.frame_ready",
            {"index": idx, "shape": tuple(frame.shape), "ts": time.time()},
        )

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                log.exception("camera.close failed")
        log.info("VisionService stopped")

    # ── Public accessors ───────────────────────────────────────────────

    def latest_frame(self) -> Optional[np.ndarray]:
        """Return the most recent frame (or None if nothing captured yet)."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def frame_index(self) -> int:
        with self._lock:
            return self._index

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_capture_still(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "path" not in payload:
            return
        path = str(payload["path"])
        try:
            self._camera.capture_still(path)
            self.bus.publish("vision.still_saved", {"path": path})
        except Exception:
            log.exception("capture_still(%s) failed", path)
            self.bus.publish("vision.error", {"reason": "still_failed"})
