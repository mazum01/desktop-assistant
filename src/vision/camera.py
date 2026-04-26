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
    width: int = 1280
    height: int = 720
    framerate: int = 30
    # Format used for stream captures; "RGB888" gives H×W×3 numpy arrays
    stream_format: str = "RGB888"


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
            controls={"FrameRate": float(self._cfg.framerate)},
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
        if not self._sim and self._cam is not None:
            self._cam.stop()
        self._running = False
        log.info("Camera %d stopped", self._cfg.index)

    def close(self) -> None:
        """Stop and close the camera, releasing the device handle."""
        self.stop()
        if not self._sim and self._cam is not None:
            self._cam.close()
            self._cam = None

    # ── Capture ─────────────────────────────────────────────────────────

    def capture_frame(self) -> np.ndarray:
        """
        Return the latest frame as a numpy array (H×W×3, uint8, RGB).
        In sim mode returns a black frame of the configured resolution.
        Raises RuntimeError if the camera has not been started.
        """
        if not self._running:
            raise RuntimeError("Camera.start() must be called before capture_frame()")
        if self._sim:
            return np.zeros(
                (self._cfg.height, self._cfg.width, 3), dtype=np.uint8
            )
        return self._cam.capture_array("main")

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
            self._cam.stop()

        still_cfg = self._cam.create_still_configuration()
        self._cam.configure(still_cfg)
        self._cam.start()
        self._cam.capture_file(path)
        self._cam.stop()

        if was_running:
            video_cfg = self._cam.create_video_configuration(
                main={
                    "size": (self._cfg.width, self._cfg.height),
                    "format": self._cfg.stream_format,
                },
                controls={"FrameRate": float(self._cfg.framerate)},
            )
            self._cam.configure(video_cfg)
            self._cam.start()

        log.info("Still saved to %s", path)

    # ── Context manager ─────────────────────────────────────────────────

    def __enter__(self) -> "Camera":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.close()
