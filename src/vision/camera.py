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
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import cv2

log = logging.getLogger(__name__)

try:
    from picamera2 import Picamera2
    _PICAMERA2_AVAILABLE = True
except ImportError:
    _PICAMERA2_AVAILABLE = False
    log.warning("picamera2 not available — camera running in simulation mode")


# Autofocus mode → libcamera AfMode integer
_AF_MODE_MAP = {
    "continuous": 2,    # Continuous AF — camera tracks focus in real-time
    "auto":       1,    # Single AF sweep triggered once at startup
    "manual":     0,    # Manual — use lens_position (diopters); 0 = ∞, 1 = 1 m
    "off":        0,    # Alias for manual with lens_position = 0 (infinity)
}


@dataclass
class CameraConfig:
    index: int = 0                  # CSI slot (0 = slot 0, 1 = slot 1)
    width: int = 640
    height: int = 480
    framerate: int = 30
    stream_format: str = "RGB888"   # libcamera/PiSP delivers BGR bytes with this label; no conversion needed
    # Software rotation applied after capture. 0–359 degrees.
    # Multiples of 90 are lossless (cv2.rotate); other angles keep the same
    # canvas size via cv2.warpAffine (corners are slightly cropped).
    rotation_deg: int = 0
    # Autofocus mode: "continuous" | "auto" | "manual" | "off"
    # "continuous" keeps both cameras in sync with the scene distance.
    # "auto"       does a single AF sweep at startup (may drift over time).
    # "manual"     locks to lens_position diopters (0 = ∞, 1.0 = 1 m, 2.0 = 0.5 m).
    af_mode: str = "continuous"
    # Lens position in diopters — only used when af_mode is "manual" or "off".
    lens_position: float = 0.0


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

        # Background capture thread state
        self._latest_array: Optional[np.ndarray] = None
        self._frame_lock = threading.Lock()
        self._capture_thread: Optional[threading.Thread] = None
        self._diag_logged = False  # log first-frame pixel info once

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
    def is_ready(self) -> bool:
        """True once the capture thread has deposited the first frame (or in sim mode)."""
        return self._running and (self._sim or self._latest_array is not None)

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

        af_int = _AF_MODE_MAP.get(self._cfg.af_mode, 2)
        controls: dict = {
            "FrameRate": float(self._cfg.framerate),
            "NoiseReductionMode": 0,  # Off — eliminates ISP NR latency
            "AfMode": af_int,
        }
        if af_int == 2:
            # Continuous: use fast AF speed to minimise lag between cameras
            controls["AfSpeed"] = 1
        elif af_int == 1:
            # Auto: trigger a single AF sweep immediately after start
            controls["AfTrigger"] = 0
        elif af_int == 0:
            # Manual / off: lock to the specified dioptre value
            controls["LensPosition"] = float(self._cfg.lens_position)

        video_cfg = self._cam.create_video_configuration(
            main={
                "size": (self._cfg.width, self._cfg.height),
                "format": self._cfg.stream_format,
            },
            buffer_count=6,  # more ISP pipeline headroom → lower capture latency
            controls=controls,
        )
        self._cam.configure(video_cfg)
        self._cam.start()
        # Brief warm-up so auto-exposure settles
        time.sleep(0.5)
        self._running = True

        # Spawn a dedicated capture thread that continuously pulls frames from
        # the ISP. This decouples ISP delivery timing from GIL contention in the
        # service tick thread — the tick just copies the latest frame immediately
        # without waiting for the ISP pipeline.
        self._capture_thread = threading.Thread(
            target=self._capture_loop,
            daemon=True,
            name=f"cam{self._cfg.index}-capture",
        )
        self._capture_thread.start()
        # Wait for the first frame so capture_frame() is immediately usable
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self._frame_lock:
                if self._latest_array is not None:
                    break
            time.sleep(0.01)
        log.info(
            "Camera %d started: %dx%d @ %dfps",
            self._cfg.index, self._cfg.width, self._cfg.height, self._cfg.framerate,
        )

    def _capture_loop(self) -> None:
        """Continuously pull frames from the ISP into _latest_array as BGR.

        libcamera v0.7 / PiSP on RPi5 uses the DRM naming convention where the
        "RGB888" format string stores bytes as B-G-R in memory (BGR), not R-G-B.
        capture_array("main") therefore already returns a BGR-ordered array —
        no channel conversion is needed before passing to imencode or cv2 draws.
        """
        while self._running:
            try:
                arr = self._cam.capture_array("main")
                if not self._running:
                    break
                # picamera2 on libcamera v0.7 / PiSP (RPi5) delivers BGR bytes in
                # memory even when the stream is configured as "RGB888" (DRM/V4L2
                # naming quirk: RGB888 = packed 24-bit with B first in memory).
                # No channel conversion needed; the raw array is already in the
                # BGR byte order that OpenCV's imencode expects.
                if not self._diag_logged:
                    log.debug(
                        "Camera %d: first frame shape=%s dtype=%s ch0=%d ch1=%d ch2=%d",
                        self._cfg.index, arr.shape, arr.dtype,
                        int(arr[0, 0, 0]), int(arr[0, 0, 1]), int(arr[0, 0, 2]),
                    )
                    self._diag_logged = True
                with self._frame_lock:
                    self._latest_array = arr
            except Exception:
                if not self._running:
                    break
                log.debug("Camera %d: capture error in loop", self._cfg.index)
                time.sleep(0.01)

    def stop(self) -> None:
        """Stop the camera stream, background capture thread, and release resources."""
        if not self._running:
            return
        self._running = False  # signal capture thread to exit
        # Stop the camera first so any blocking capture_array() call unblocks
        if not self._sim and self._cam is not None:
            self._cam.stop()
        if self._capture_thread is not None:
            self._capture_thread.join(timeout=2.0)
            self._capture_thread = None
        with self._frame_lock:
            self._latest_array = None
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
        Return the latest captured frame as a numpy array (H×W×3, uint8, BGR).

        Acquires the frame lock briefly to copy the latest DMA buffer to heap
        memory. The DMA→heap copy (≈28ms due to page-fault warmup) happens here
        so that only one thread reads from DMA memory at a time, avoiding the
        cache-contention latency caused by concurrent DMA reads.
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
        with self._frame_lock:
            if self._latest_array is None:
                raise RuntimeError("Camera.start() must be called before capture_frame()")
            return self._latest_array.copy()

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
