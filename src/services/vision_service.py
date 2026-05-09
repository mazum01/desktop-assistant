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

Public accessors (in-process callers):
    svc.latest_frame() → np.ndarray | None   (RGB, for detection)
    svc.latest_jpeg()  → bytes | None        (pre-encoded JPEG, for streaming)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import cv2
import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)


class VisionService(Service):
    name = "vision"
    tick_seconds = 0.033   # ~30 fps frame-publish cadence; matches camera framerate

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        camera=None,
        camera_config=None,
    ) -> None:
        super().__init__(bus=bus)
        self._camera = camera
        self._camera_config = camera_config
        self._latest: Optional[np.ndarray] = None
        self._latest_jpeg: Optional[bytes] = None
        self._index = 0
        self._lock = threading.Lock()
        self._unsubs = []

    def on_start(self) -> None:
        if self._camera is None:
            from src.vision.camera import Camera, CameraConfig
            cfg = self._camera_config or CameraConfig()
            self._camera = Camera(cfg)
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

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._camera, "hardware_ready", False))

    def run_tick(self) -> None:
        if self._camera is None:
            return
        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("capture_frame failed")
            self.bus.publish("vision.error", {"reason": "capture_failed"})
            return

        # Pre-encode JPEG here (off the bus-callback thread) so WebService
        # can consume it without any additional copy or encode work.
        bgr = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
        ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, 60])
        jpeg = bytes(buf) if ok else None

        with self._lock:
            self._latest = frame
            self._latest_jpeg = jpeg
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
        """Return the most recent frame in RGB (or None). Used by detection services."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def latest_jpeg(self) -> Optional[bytes]:
        """Return the most recent pre-encoded JPEG bytes (or None). Used by WebService."""
        with self._lock:
            return self._latest_jpeg

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
