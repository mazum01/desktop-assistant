"""
Vision service.

Owns a `Camera` instance, runs a continuous capture loop, exposes the
latest frame to in-process subscribers, and publishes lightweight
frame metadata on the bus so other services can react without us
spamming megabyte-sized payloads through the pub/sub layer.

Subscribes to perception events and draws detection overlays (face ovals,
object boxes) directly onto each JPEG frame before streaming so the
overlays are always pixel-accurate and in sync with the video.

Topics published:
    vision.frame_ready  {"index": int, "shape": (H, W, C), "ts": float}
    vision.error        {"reason": str}

Topics subscribed:
    vision.capture_still  {"path": str}     — write a JPEG still to *path*
    perception.faces      — caches face bboxes for overlay drawing
    perception.objects    — caches object bboxes for overlay drawing

Public accessors (in-process callers):
    svc.latest_frame() → np.ndarray | None   (BGR, for detection)
    svc.latest_jpeg()  → bytes | None        (pre-encoded JPEG, for streaming)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

import cv2
import numpy as np

from src.core.bus import MessageBus
from src.core.service import Service

log = logging.getLogger(__name__)

# Object box colour (BGR)
_CYAN = (255, 212, 0)   # #00d4ff

# Distinct face colours (BGR) — visually separated, readable on camera backgrounds
_FACE_COLORS = [
    (  0, 255, 136),   # green
    (255, 100,   0),   # blue
    (  0, 100, 255),   # red-orange
    (255,   0, 200),   # magenta
    (  0, 220, 255),   # yellow
    (200, 255,   0),   # lime
    (255, 160,  50),   # sky blue
    (128,   0, 255),   # purple
]


def _rotate_frame(frame: np.ndarray, degrees: int) -> np.ndarray:
    """Rotate *frame* by *degrees* clockwise.

    Multiples of 90° use cv2.rotate (lossless, may change dimensions).
    Other angles use cv2.warpAffine centred on the frame, keeping the same
    output canvas (corners are clipped by the rotation).
    """
    deg = degrees % 360
    if deg == 0:
        return frame
    if deg == 90:
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if deg == 180:
        return cv2.rotate(frame, cv2.ROTATE_180)
    if deg == 270:
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    h, w = frame.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), -float(deg), 1.0)
    return cv2.warpAffine(frame, M, (w, h))


def _face_color(face_id: str | None, index: int) -> tuple:
    """Return a consistent BGR colour for a face.

    Uses face_id hash for stability (same person → same colour across frames);
    falls back to round-robin index when no id is available.
    """
    if face_id:
        return _FACE_COLORS[hash(face_id) % len(_FACE_COLORS)]
    return _FACE_COLORS[index % len(_FACE_COLORS)]


def _draw_overlays(frame_bgr: np.ndarray, faces: list, objects: list) -> None:
    """Draw face ovals and object rectangles in-place on a BGR frame."""
    for idx, face in enumerate(faces):
        bbox = face.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        color = _face_color(face.get("face_id"), idx)
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        # Padded oval to fully surround the face
        pw = max(4, int((x2 - x1) * 0.15))
        ph = max(4, int((y2 - y1) * 0.20))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        rx = max(1, (x2 - x1) // 2 + pw)
        ry = max(1, (y2 - y1) // 2 + ph)
        cv2.ellipse(frame_bgr, (cx, cy), (rx, ry), 0, 0, 360, color, 2, cv2.LINE_AA)
        label = face.get("name") or (face.get("face_id") and "unknown")
        if label:
            lx = max(0, cx - 20)
            ly = max(10, cy - ry - 4)
            cv2.putText(frame_bgr, label, (lx, ly),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)

    for obj in objects:
        bbox = obj.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), _CYAN, 1, cv2.LINE_AA)
        conf  = obj.get("confidence", 0)
        label = f"{obj.get('label', '?')} {int(conf * 100)}%"
        ly    = max(10, y1 - 4)
        cv2.putText(frame_bgr, label, (x1, ly),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.4, _CYAN, 1, cv2.LINE_AA)


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
        # Detection caches — written by bus callbacks, read in run_tick
        self._det_lock = threading.Lock()
        self._latest_faces: List[dict] = []
        self._latest_objects: List[dict] = []
        self._unsubs = []
        # Rotation — initialised from camera config, updated live via bus
        self._rotation_lock = threading.Lock()
        _init_rot = 0
        if camera_config is not None:
            _init_rot = int(getattr(camera_config, "rotation_deg", 0))
        self._rotation_deg: int = _init_rot % 360

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
        self._unsubs.append(
            self.bus.subscribe("perception.faces", self._on_faces)
        )
        self._unsubs.append(
            self.bus.subscribe("perception.objects", self._on_objects)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_rotation", self._on_set_rotation)
        )
        log.info(
            "VisionService started; hardware_ready=%s",
            getattr(self._camera, "hardware_ready", False),
        )

    @property
    def hardware_ready(self) -> bool:
        return bool(getattr(self._camera, "hardware_ready", False))

    @property
    def rotation_deg(self) -> int:
        with self._rotation_lock:
            return self._rotation_deg

    def run_tick(self) -> None:
        if self._camera is None:
            return
        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("capture_frame failed")
            self.bus.publish("vision.error", {"reason": "capture_failed"})
            return

        # Apply software rotation before any processing or display.
        with self._rotation_lock:
            rot = self._rotation_deg
        if rot:
            frame = _rotate_frame(frame, rot)

        # picamera2 "RGB888" actually delivers BGR in the buffer.
        # Snapshot detection caches while we have the lock.
        with self._det_lock:
            faces   = self._latest_faces
            objects = self._latest_objects

        # Draw overlays then encode. Work on a copy so latest_frame()
        # consumers always see a clean (no-overlay) frame.
        display = frame.copy()
        _draw_overlays(display, faces, objects)
        ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 60])
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
        """Return the most recent raw frame (BGR, no overlay). Used by detection services."""
        with self._lock:
            return None if self._latest is None else self._latest.copy()

    def latest_jpeg(self) -> Optional[bytes]:
        """Return the most recent pre-encoded JPEG (with overlays). Used by WebService."""
        with self._lock:
            return self._latest_jpeg

    def frame_index(self) -> int:
        with self._lock:
            return self._index

    # ── Bus handlers ───────────────────────────────────────────────────

    def _on_faces(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._det_lock:
            self._latest_faces = list(payload.get("faces", []))

    def _on_objects(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._det_lock:
            self._latest_objects = list(payload.get("objects", []))

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

    def _on_set_rotation(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "rotation_deg" not in payload:
            return
        deg = int(payload["rotation_deg"]) % 360
        with self._rotation_lock:
            self._rotation_deg = deg
        log.info("Camera rotation set to %d°", deg)
        self.bus.publish("camera.rotation_changed", {"rotation_deg": deg})
