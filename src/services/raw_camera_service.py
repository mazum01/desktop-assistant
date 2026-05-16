"""
Raw Camera Service — lightweight second-camera capture and MJPEG publishing.

Captures frames from a secondary CSI camera (index 1 by default), encodes
them to JPEG, and publishes ``vision.frame2_ready`` on the bus so the
WebService can serve a ``/stream2`` MJPEG endpoint.

No face detection, object detection, or overlay drawing is performed — this
service is intentionally minimal to keep CPU overhead low.  Both cameras run
independent continuous autofocus.

Topics subscribed:
    camera2.set_rotation          {"rotation_deg": int}  — live rotation update
    camera.set_resolution         {"width": int, "height": int}
    camera.set_stream_resolution  {"width": int, "height": int}

Topics published:
    vision.frame2_ready        {"index": int, "ts": float}
    camera2.rotation_changed   {"rotation_deg": int}
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2

from src.core.service import Service
from src.core.bus import MessageBus

log = logging.getLogger(__name__)

_DEFAULT_JPEG_QUALITY = 55  # lower than primary cam to save bandwidth


@dataclass
class RawCameraConfig:
    index: int = 1
    width: int = 640
    height: int = 480
    framerate: int = 30        # matches primary cam; Pi 5 handles dual 30fps
    rotation_deg: int = 0
    jpeg_quality: int = _DEFAULT_JPEG_QUALITY
    af_mode: str = "continuous"
    lens_position: float = 0.0
    # MJPEG stream output resolution — camera captures at width×height for full FOV;
    # encoder downscales to stream_width×stream_height before JPEG encoding.
    stream_width: int = 0
    stream_height: int = 0


class RawCameraService(Service):
    """Capture-only service for the second CSI camera."""

    name = "raw_camera2"
    # tick_seconds is computed from framerate in on_start; default ~30fps
    tick_seconds = 0.033

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        camera_config: Optional[RawCameraConfig] = None,
        camera=None,
    ) -> None:
        super().__init__(bus=bus)
        self._cam_cfg = camera_config or RawCameraConfig()
        self._camera = camera

        self._lock = threading.Lock()
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame: Optional["np.ndarray"] = None
        self._index = 0
        self._rotation_deg: int = self._cam_cfg.rotation_deg % 360
        self._stream_width: int = self._cam_cfg.stream_width
        self._stream_height: int = self._cam_cfg.stream_height

        # Focus sync state: tracks when we last received a focused lens position.
        # (Reserved for potential future use.)

        # Derived tick rate from config
        if self._cam_cfg.framerate > 0:
            self.tick_seconds = 1.0 / self._cam_cfg.framerate

    # ── Lifecycle ───────────────────────────────────────────────────────

    def on_start(self) -> None:
        if self._camera is None:
            from src.vision.camera import Camera, CameraConfig
            cfg = CameraConfig(
                index=self._cam_cfg.index,
                width=self._cam_cfg.width,
                height=self._cam_cfg.height,
                framerate=self._cam_cfg.framerate,
                rotation_deg=self._cam_cfg.rotation_deg,
                af_mode=self._cam_cfg.af_mode,
                lens_position=self._cam_cfg.lens_position,
            )
            self._camera = Camera(cfg)
        try:
            self._camera.start()
        except Exception:
            log.exception("RawCameraService: camera.start() failed")

        if self.bus:
            self.bus.subscribe("camera2.set_rotation", self._on_set_rotation)
            self.bus.subscribe("camera.set_resolution", self._on_set_resolution)
            self.bus.subscribe("camera.set_stream_resolution", self._on_set_stream_resolution)

        log.info(
            "RawCameraService started; cam_index=%d hw_ready=%s %dx%d@%dfps",
            self._cam_cfg.index,
            getattr(self._camera, "hardware_ready", False),
            self._cam_cfg.width,
            self._cam_cfg.height,
            self._cam_cfg.framerate,
        )

    def on_stop(self) -> None:
        if self._camera is not None:
            try:
                self._camera.close()
            except Exception:
                log.exception("RawCameraService: camera.close() failed")

    # ── Tick ────────────────────────────────────────────────────────────

    def run_tick(self) -> None:
        if self._camera is None:
            return

        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("RawCameraService: capture_frame failed")
            return

        with self._lock:
            rot = self._rotation_deg

        if rot:
            frame = _rotate_frame(frame, rot)

        sw, sh = self._stream_width, self._stream_height
        fh, fw = frame.shape[:2]
        if sw > 0 and sh > 0 and (fw > sw or fh > sh):
            ar = fw / fh
            tw = sw
            th = round(sw / ar)
            if th > sh:
                th = sh
                tw = round(sh * ar)
            frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)

        quality = self._cam_cfg.jpeg_quality
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        jpeg = bytes(buf) if ok else None

        with self._lock:
            self._latest_jpeg = jpeg
            self._latest_frame = frame
            self._index += 1
            idx = self._index

        self.bus.publish("vision.frame2_ready", {"index": idx, "ts": time.time()})

    # ── Public accessors ─────────────────────────────────────────────────

    def latest_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._latest_jpeg

    def latest_frame(self) -> Optional["np.ndarray"]:
        """Return the most recent raw BGR frame, or None if unavailable."""
        with self._lock:
            return self._latest_frame

    @property
    def rotation_deg(self) -> int:
        with self._lock:
            return self._rotation_deg

    @property
    def resolution(self) -> tuple:
        with self._lock:
            return (self._cam_cfg.width, self._cam_cfg.height)

    @property
    def stream_resolution(self) -> tuple:
        return (self._stream_width, self._stream_height)

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._camera, "hardware_ready", False))

    # ── Bus handlers ─────────────────────────────────────────────────────

    def _on_set_rotation(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "rotation_deg" not in payload:
            return
        deg = int(payload["rotation_deg"]) % 360
        with self._lock:
            self._rotation_deg = deg
        if self.bus:
            self.bus.publish("camera2.rotation_changed", {"rotation_deg": deg})
        log.info("RawCameraService: rotation set to %d°", deg)

    def _on_set_resolution(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "width" not in payload or "height" not in payload:
            return
        w = int(payload["width"])
        h = int(payload["height"])
        if self._camera is None:
            return
        try:
            self._cam_cfg = RawCameraConfig(
                index=self._cam_cfg.index,
                width=w,
                height=h,
                framerate=self._cam_cfg.framerate,
                rotation_deg=self._cam_cfg.rotation_deg,
                jpeg_quality=self._cam_cfg.jpeg_quality,
                stream_width=self._stream_width,
                stream_height=self._stream_height,
            )
            self._camera.set_resolution(w, h)
            log.info("Camera 2 resolution changed to %dx%d", w, h)
        except Exception:
            log.exception("Failed to change camera 2 resolution to %dx%d", w, h)

    def _on_set_stream_resolution(self, _topic, payload) -> None:
        """Update cam2 MJPEG stream downscale target without restarting the camera."""
        if not isinstance(payload, dict) or "width" not in payload or "height" not in payload:
            return
        self._stream_width = int(payload["width"])
        self._stream_height = int(payload["height"])
        log.info("Camera 2 stream resolution changed to %dx%d", self._stream_width, self._stream_height)


# ── Helpers ──────────────────────────────────────────────────────────────

def _rotate_frame(frame, deg: int):
    """Apply software rotation — same logic as VisionService."""
    deg = deg % 360
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    # Arbitrary angle — warpAffine (slight corner crop)
    import numpy as np
    h, w = frame.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), -deg, 1.0)
    return cv2.warpAffine(frame, M, (w, h))
