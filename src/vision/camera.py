"""
Camera driver for the Raspberry Pi Camera Module 3 Wide.

Uses picamera2 (libcamera backend, Pi OS Bookworm standard).
Supports single-camera bring-up now; a second camera index can be
passed via CameraConfig.index when the second module is installed.

Gracefully falls back to simulation mode if picamera2 is unavailable
or the camera is not detected — identical pattern to ServoController.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2
    _PICAMERA2_AVAILABLE = True
except ImportError:
    _PICAMERA2_AVAILABLE = False
    log.warning("picamera2 not available — camera running in simulation mode")


@dataclass
class CameraConfig:
    index: int = 0                  # CSI slot (0 = slot 0, 1 = slot 1)
    width: int = 640
    height: int = 480
    framerate: int = 30
    stream_format: str = "BGR888"   # native cv2 byte order; was RGB888
    # Software rotation applied after capture. 0–359 degrees.
    # Multiples of 90 are lossless (cv2.rotate); other angles keep the same
    # canvas size via cv2.warpAffine (corners are slightly cropped).
    rotation_deg: int = 0


class Camera:
    """
    Thin wrapper around Picamera2 for frame capture.

    Usage (hardware):
        cam = Camera()
        cam.start()
        frame = cam.capture_frame()   # numpy array H×W×3 uint8
        cam.stop()

    Usage (context manager):
        with Camera() as cam:
            frame = cam.capture_frame()
    """

    def __init__(self, config: Optional[CameraConfig] = None) -> None:
        self._cfg = config or CameraConfig()
        self._cam: Optional[object] = None
        self._running = False
        self._sim = False

        if not _PICAMERA2_AVAILABLE:
            log.warning("[sim] picamera2 not installed — camera in sim mode")
            self._sim = True
            return

        try:
            cameras = Picamera2.global_camera_info()
            if not cameras:
                log.warning("[sim] No cameras detected — camera in sim mode")
                self._sim = True
                return
            # Find the camera at the requested CSI index
            match = [c for c in cameras if c.get("Num", c.get("num", -1)) == self._cfg.index]
            if not match:
                log.warning(
                    "[sim] Camera index %d not found (available: %s) — sim mode",
                    self._cfg.index,
                    [c.get("Num", c.get("num")) for c in cameras],
                )
                self._sim = True
                return
            self._cam = Picamera2(self._cfg.index)
        except Exception as exc:
            log.warning("[sim] Camera init failed (%s) — sim mode", exc)
            self._sim = True

    # ── Properties ──────────────────────────────────────────────────────

    @property
    def hardware_ready(self) -> bool:
        return not self._sim

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def config(self) -> CameraConfig:
        return self._cfg

    @property
    def resolution(self) -> tuple:
        return (self._cfg.width, self._cfg.height)

    # ── Lifecycle ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Configure and start the camera stream."""
        if self._running:
            return
        if self._sim:
            log.debug("[sim] camera.start() — no-op in sim mode")
            self._running = True
            return

        video_cfg = self._cam.create_video_configuration(
            main={
                "size": (self._cfg.width, self._cfg.height),
                "format": self._cfg.stream_format,
            },
            buffer_count=6,  # more ISP pipeline headroom → lower capture latency
            controls={
                "FrameRate": float(self._cfg.framerate),
                "NoiseReductionMode": 0,  # Off — eliminates ISP NR latency
            },
        )
        self._cam.configure(video_cfg)
        self._cam.start()
        # Brief warm-up so auto-exposure settles
        time.sleep(0.5)
        self._running = True
        log.info(
            "Camera %d started: %dx%d @ %dfps",
            self._cfg.index, self._cfg.width, self._cfg.height, self._cfg.framerate,
        )

    def stop(self) -> None:
        """Stop the camera stream and release resources."""
        if not self._running:
            return
        self._running = False
        if not self._sim and self._cam is not None:
            self._cam.stop()
        log.info("Camera %d stopped", self._cfg.index)

    def set_resolution(self, width: int, height: int) -> None:
        """Change capture resolution. Stops and restarts the stream if running."""
        if self._cfg.width == width and self._cfg.height == height:
            return
        self._cfg = CameraConfig(
            index=self._cfg.index,
            width=width,
            height=height,
            framerate=self._cfg.framerate,
            stream_format=self._cfg.stream_format,
            rotation_deg=self._cfg.rotation_deg,
        )
        if self._sim:
            log.info("[sim] Resolution changed to %dx%d", width, height)
            return
        was_running = self._running
        if was_running:
            self.stop()
            self.start()
        log.info("Camera %d resolution changed to %dx%d", self._cfg.index, width, height)

    def close(self) -> None:
        """Stop and close the camera, releasing the device handle."""
        self.stop()
        if not self._sim and self._cam is not None:
            self._cam.close()
            self._cam = None

    # ── Capture ─────────────────────────────────────────────────────────

    def capture_frame(self) -> np.ndarray:
        """
        Capture and return a frame as a numpy array (H×W×3, uint8, BGR).

        Blocks until the ISP delivers the next frame (~1/framerate seconds).
        With NoiseReductionMode=0 this is typically ≤33ms at 30fps.
        Returns a heap-resident copy so the caller can modify it freely.
        In sim mode returns a synthetic test frame.
        Raises RuntimeError if the camera has not been started.
        """
        if not self._running:
            raise RuntimeError("Camera.start() must be called before capture_frame()")
        if self._sim:
            import cv2
            frame = np.full(
                (self._cfg.height, self._cfg.width, 3), 40, dtype=np.uint8
            )
            ts = time.strftime("%H:%M:%S")
            cv2.putText(frame, "CAMERA  SIM  MODE", (50, self._cfg.height // 2 - 40),
                        cv2.FONT_HERSHEY_DUPLEX, 2.0, (100, 160, 220), 3, cv2.LINE_AA)
            cv2.putText(frame, ts, (50, self._cfg.height // 2 + 40),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.4, (180, 180, 180), 2, cv2.LINE_AA)
            cv2.rectangle(frame, (20, 20), (self._cfg.width - 20, self._cfg.height - 20),
                          (60, 80, 100), 2)
            return frame
        arr = self._cam.capture_array("main")
        # Copy immediately to heap memory — picamera2 may return a view into a
        # DMA/mmap buffer; this ensures callers read from warm CPU cache and
        # the ISP buffer is released back to libcamera as soon as possible.
        return arr.copy()

    def capture_still(self, path: str) -> None:
        """
        Capture a full-resolution JPEG still to *path*.
        Temporarily switches to a still configuration then resumes video.
        In sim mode writes nothing but logs the call.
        """
        if self._sim:
            log.debug("[sim] capture_still(%s) — no-op in sim mode", path)
            return
        was_running = self._running
        if was_running:
            self.stop()

        still_cfg = self._cam.create_still_configuration()
        self._cam.configure(still_cfg)
        self._cam.start()
        self._cam.capture_file(path)
        self._cam.stop()

        if was_running:
            self.start()

        log.info("Still saved to %s", path)

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "Camera":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.close()
