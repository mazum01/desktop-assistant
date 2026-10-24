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
import math
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

try:
    from libcamera import Transform as _Transform
except ImportError:
    _Transform = None


# Square input edge of the Hailo detection models (SCRFD, YOLO).
DETECTOR_INPUT_SIZE = 640


def detector_input_size(width: int, height: int) -> Optional[tuple]:
    """Return the (w, h) a 640×640 letterbox resizes a *width*×*height* frame to.

    Matches the truncation used by the detectors so a frame of exactly this
    size can be pasted into the letterbox without another resize. Returns None
    when the frame is not larger than the model input or the size is odd
    (the ISP needs even dimensions).
    """
    if width <= 0 or height <= 0:
        return None
    scale = min(DETECTOR_INPUT_SIZE / width, DETECTOR_INPUT_SIZE / height)
    if scale >= 1.0:
        return None
    w, h = int(width * scale), int(height * scale)
    if w % 2 or h % 2:
        return None
    return (w, h)


def _hw_rotation_for(deg: int) -> int:
    """Return the part of *deg* the sensor can apply for free (0 or 180)."""
    return 180 if deg % 360 == 180 else 0


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
    # Requested rotation, 0–359 degrees. 180° is applied by the sensor
    # (horizontal + vertical flip); services rotate the remainder in software.
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
    # MJPEG stream resolution — resize frame before JPEG encoding for web delivery.
    # 0 means use the full capture resolution. Set smaller (e.g., 640×480) to
    # decouple encode cost from capture resolution and reduce GIL pressure.
    stream_width: int = 0
    stream_height: int = 0


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
        # Rotation the sensor is currently applying (0 or 180).
        self._hw_rotation_deg = 0

        # Background capture thread state
        self._latest_array: Optional[np.ndarray] = None
        # Detector-sized copy of each frame scaled by the ISP ("lores" stream).
        self._latest_small: Optional[np.ndarray] = None
        self._small_size: Optional[tuple] = None
        self._frame_lock = threading.Lock()
        self._capture_thread: Optional[threading.Thread] = None
        self._diag_logged = False  # log first-frame pixel info once
        # Lens position and AF state read from ISP metadata (diopters, -1 = unknown)
        # AfState: 0=Idle, 1=Scanning, 2=Focused, 3=Failed
        self._lens_position: float = -1.0
        self._af_state: int = -1
        self._lens_lock = threading.Lock()

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

    @property
    def small_size(self) -> Optional[tuple]:
        """(w, h) of the ISP-scaled detector frame, or None when unavailable."""
        return self._small_size

    @property
    def hw_rotation_deg(self) -> int:
        """Rotation already applied by the sensor to delivered frames."""
        return self._hw_rotation_deg

    def set_rotation(self, deg: int) -> None:
        """Record the requested rotation; reconfigure the sensor flip if it changes."""
        deg = int(deg) % 360
        self._cfg.rotation_deg = deg
        if self._sim or self._cam is None:
            return
        if _hw_rotation_for(deg) == self._hw_rotation_deg or not self._running:
            return
        self.stop()
        self.start()
        log.info("Camera %d sensor rotation set to %d°", self._cfg.index, self._hw_rotation_deg)

    @property
    def current_lens_position(self) -> Optional[float]:
        """Last known lens position in diopters, only when cam is focused.

        Returns None while AF is scanning (AfState=1) or not yet ready.
        This prevents broadcasting an unstable/transient position during
        focus hunts, so downstream cameras don't lock to garbage data.
        """
        with self._lens_lock:
            if self._lens_position < 0:
                return None          # no metadata yet
            if self._af_state == 1:
                return None          # scanning — position is unstable
            return self._lens_position

    def set_lens_position(self, position: float) -> None:
        """Switch to manual AF and lock to *position* diopters (0 = ∞, 1 = 1 m).
        Thread-safe; can be called from any thread while the camera is running."""
        if self._sim or self._cam is None or not self._running:
            return
        try:
            self._cam.set_controls({"AfMode": 0, "LensPosition": float(position)})
        except Exception as exc:
            log.debug("Camera %d: set_lens_position(%.3f) failed: %s",
                      self._cfg.index, position, exc)

    def set_continuous_af(self) -> None:
        """Return the camera to continuous autofocus mode.
        Thread-safe; used by RawCameraService when focus-sync updates stop arriving."""
        if self._sim or self._cam is None or not self._running:
            return
        try:
            self._cam.set_controls({"AfMode": 2, "AfSpeed": 1})
        except Exception as exc:
            log.debug("Camera %d: set_continuous_af() failed: %s", self._cfg.index, exc)

    def get_focus(self) -> dict:
        controls = self._cam.camera_controls if not self._sim and self._cam else {}
        limits = controls.get("LensPosition", (0.0, 0.0, 0.0))
        return {
            "supported": self.hardware_ready and "AfMode" in controls and "LensPosition" in controls,
            "af_mode": self._cfg.af_mode,
            "lens_position": self._cfg.lens_position,
            "current_lens_position": self.current_lens_position,
            "min_position": float(limits[0]),
            "max_position": float(limits[1]),
        }

    def set_focus(self, af_mode: str, lens_position: float) -> dict:
        """Apply focus controls without restarting capture; propagate hardware errors."""
        if af_mode not in _AF_MODE_MAP:
            raise ValueError("invalid autofocus mode")
        if isinstance(lens_position, bool) or not isinstance(lens_position, (int, float)):
            raise ValueError("lens_position must be a finite number")
        status = self.get_focus()
        if not status["supported"] or not self._running:
            raise RuntimeError("camera focus control unavailable")
        if not math.isfinite(lens_position) or not status["min_position"] <= lens_position <= status["max_position"]:
            raise ValueError("lens_position outside camera control range")
        mode = _AF_MODE_MAP[af_mode]
        controls: dict[str, int | float] = {"AfMode": mode}
        if mode == 0:
            controls["LensPosition"] = float(lens_position)
        elif mode == 1:
            controls["AfTrigger"] = 0
        else:
            controls["AfSpeed"] = 1
        self._cam.set_controls(controls)
        self._cfg.af_mode = af_mode
        self._cfg.lens_position = float(lens_position)
        return self.get_focus()

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

        video_kwargs = dict(
            main={
                "size": (self._cfg.width, self._cfg.height),
                "format": self._cfg.stream_format,
            },
            buffer_count=6,  # more ISP pipeline headroom → lower capture latency
            controls=controls,
        )
        self._configure_with_rotation(self._cam.create_video_configuration, video_kwargs)
        # Install a pre-callback to extract ISP metadata (including LensPosition)
        # on every frame.  This runs on picamera2's internal thread and is safe
        # to call concurrently with capture_array().
        self._cam.pre_callback = self._on_frame_metadata
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
            "Camera %d started: %dx%d @ %dfps (sensor rotation %d°, ISP small frame %s)",
            self._cfg.index, self._cfg.width, self._cfg.height, self._cfg.framerate,
            self._hw_rotation_deg, self._small_size,
        )

    def _configure_with_rotation(self, factory, kwargs: dict) -> None:
        """Configure the camera, preferring the sensor flip and an ISP-scaled
        detector stream, and falling back to software for whichever fails."""
        flip = _hw_rotation_for(self._cfg.rotation_deg) if _Transform is not None else 0
        small = detector_input_size(self._cfg.width, self._cfg.height)
        attempts = [(f, sm) for f in ((flip, 0) if flip else (0,))
                    for sm in ((small, None) if small else (None,))]
        last_exc: Optional[Exception] = None
        for f, sm in attempts:
            extra: dict = {}
            if f:
                extra["transform"] = _Transform(hflip=1, vflip=1)
            if sm:
                extra["lores"] = {"size": sm, "format": self._cfg.stream_format}
            try:
                self._cam.configure(factory(**extra, **kwargs))
            except Exception as exc:
                last_exc = exc
                log.warning("Camera %d: configure with flip=%s small=%s failed (%s)",
                            self._cfg.index, bool(f), sm, exc)
                continue
            self._hw_rotation_deg = f
            self._small_size = sm
            if (flip and not f) or (small and not sm):
                log.warning("Camera %d: using software fallback (sensor flip=%s, ISP small frame=%s)",
                            self._cfg.index, bool(f), sm)
            return
        raise last_exc  # type: ignore[misc]

    def _on_frame_metadata(self, request) -> None:
        """picamera2 pre_callback: extract LensPosition and AfState from every ISP request.
        Called on picamera2's internal thread; must not block."""
        try:
            meta = request.get_metadata()
            lp = meta.get("LensPosition")
            af = meta.get("AfState", -1)
            with self._lens_lock:
                if self._lens_position < 0 and lp is not None:
                    log.info("Camera %d: pre_callback active; LensPosition=%.3f AfState=%s",
                             self._cfg.index, lp, af)
                if lp is not None:
                    self._lens_position = float(lp)
                if af >= 0:
                    self._af_state = int(af)
        except Exception as exc:
            log.debug("Camera %d: _on_frame_metadata error: %s", self._cfg.index, exc)

    def _capture_loop(self) -> None:
        """Continuously pull frames from the ISP via capture_array().

        Metadata (including LensPosition) is extracted asynchronously through
        _on_frame_metadata(), which is installed as a picamera2 pre_callback.

        libcamera v0.7 / PiSP on RPi5 uses the DRM convention where the
        "RGB888" format stores bytes as B-G-R in memory (BGR), not R-G-B.
        No channel conversion is needed before passing to imencode or cv2.
        """
        while self._running:
            try:
                small = None
                if self._small_size:
                    (arr, small), _ = self._cam.capture_arrays(["main", "lores"])
                    small.flags.writeable = False
                else:
                    arr = self._cam.capture_array("main")
                if not self._running:
                    break

                if not self._diag_logged:
                    log.debug(
                        "Camera %d: first frame shape=%s dtype=%s ch0=%d ch1=%d ch2=%d",
                        self._cfg.index, arr.shape, arr.dtype,
                        int(arr[0, 0, 0]), int(arr[0, 0, 1]), int(arr[0, 0, 2]),
                    )
                    self._diag_logged = True

                # capture_array() already returns a private copy of the DMA
                # buffer; freezing it lets consumers share it without copying.
                arr.flags.writeable = False
                with self._frame_lock:
                    self._latest_array = arr
                    self._latest_small = small
                    self._latest_frame_monotonic = time.monotonic()
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
            self._latest_small = None
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
            af_mode=self._cfg.af_mode,
            lens_position=self._cfg.lens_position,
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

        The frame is the capture thread's heap copy of the DMA buffer, shared
        read-only between callers; copy it before modifying.
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
            return self._latest_array

    def capture_observation(self):
        """Return an atomic frame/availability-time snapshot, not shaft feedback.

        The frame is shared and read-only; copy it before modifying.
        """
        if self._sim:
            return self.capture_frame(), time.monotonic()
        if not self._running:
            raise RuntimeError("Camera.start() must be called before capture")
        with self._frame_lock:
            if self._latest_array is None:
                raise RuntimeError("Camera has no frame")
            return self._latest_array, self._latest_frame_monotonic

    def capture_frames(self):
        """Return (frame, small, availability time) from the same ISP request.

        *small* is the ISP-scaled detector frame of size :attr:`small_size`, or
        None when the camera is not producing one. Both are shared read-only.
        """
        if self._sim:
            return self.capture_frame(), None, time.monotonic()
        if not self._running:
            raise RuntimeError("Camera.start() must be called before capture")
        with self._frame_lock:
            if self._latest_array is None:
                raise RuntimeError("Camera has no frame")
            return self._latest_array, self._latest_small, self._latest_frame_monotonic

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
