"""
Vision service.

Owns a `Camera` instance, runs a continuous capture loop, exposes the
latest frame to in-process subscribers, and publishes lightweight
frame metadata on the bus so other services can react without us
spamming megabyte-sized payloads through the pub/sub layer.

Architecture: two-thread pipeline to decouple capture rate from encode rate.
  • run_tick() (service thread) — capture frame (~2ms), store `self._latest`,
    publish `vision.frame_ready` so Hailo inference can start immediately.
  • _encoder_loop() (encoder thread) — copy + draw overlays + JPEG encode,
    then publish `vision.jpeg_ready` so the MJPEG stream delivers the new frame.
This avoids blocking the capture loop with GIL-contended cv2 operations
(copy 7-54ms, draw 17-95ms, encode 2-27ms) and pushes cam1 from ~11fps to
closer to the 30fps ISP delivery rate.

Topics published:
    vision.frame_ready    {"index": int, "shape": (H, W, C), "ts": float}
    vision.jpeg_ready     {"index": int}
    vision.error          {"reason": str}
    vision.lens_position  {"position": float}  — cam0 lens position (diopters), ~2 Hz

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
import math
import queue
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


def _put_text_outlined(
    frame: np.ndarray,
    text: str,
    org: tuple,
    font: int,
    font_scale: float,
    color: tuple,
    thickness: int,
) -> None:
    """Draw *text* with a black outline for readability on any background."""
    outline_thick = thickness + max(2, round(thickness * 1.5))
    cv2.putText(frame, text, org, font, font_scale, (0, 0, 0), outline_thick, cv2.LINE_AA)
    cv2.putText(frame, text, org, font, font_scale, color, thickness, cv2.LINE_AA)


def _face_color(face_id: str | None, index: int) -> tuple:
    """Return a consistent BGR colour for a face.

    Uses face_id hash for stability (same person → same colour across frames);
    falls back to round-robin index when no id is available.
    """
    if face_id:
        return _FACE_COLORS[hash(face_id) % len(_FACE_COLORS)]
    return _FACE_COLORS[index % len(_FACE_COLORS)]


def _draw_overlays(frame_bgr: np.ndarray, faces: list, objects: list) -> None:
    """Draw face ovals and object rectangles in-place on a BGR frame.

    All pixel dimensions scale with the frame resolution relative to a 640×480
    baseline so overlays look the same physical size regardless of capture res.
    """
    h, w = frame_bgr.shape[:2]
    scale = min(w / 640.0, h / 480.0)

    for idx, face in enumerate(faces):
        bbox = face.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        color = _face_color(face.get("face_id"), idx)
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        # Padded oval to fully surround the face
        pw = max(2, int((x2 - x1) * 0.15))
        ph = max(2, int((y2 - y1) * 0.20))
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        rx = max(1, (x2 - x1) // 2 + pw)
        ry = max(1, (y2 - y1) // 2 + ph)
        ellipse_thickness = max(1, round(2 * scale))
        cv2.ellipse(frame_bgr, (cx, cy), (rx, ry), 0, 0, 360, color, ellipse_thickness, cv2.LINE_AA)
        label = face.get("name") or (face.get("face_id") and "unknown")
        if label:
            font_scale = max(0.8, 1.1 * scale)
            font_thick = max(1, round(2 * scale))
            lx = max(0, cx - 20)
            ly = max(10, cy - ry - 4)
            _put_text_outlined(frame_bgr, label, (lx, ly),
                               cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, font_thick)

    for obj in objects:
        bbox = obj.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        box_thick = max(1, round(scale))
        cv2.rectangle(frame_bgr, (x1, y1), (x2, y2), _CYAN, box_thick, cv2.LINE_AA)
        conf  = obj.get("confidence", 0)
        label = f"{obj.get('label', '?')} {int(conf * 100)}%"
        ly    = max(10, y1 - 4)
        font_scale = max(0.8, 1.1 * scale)
        font_thick = max(1, round(2 * scale))
        _put_text_outlined(frame_bgr, label, (x1, ly),
                           cv2.FONT_HERSHEY_SIMPLEX, font_scale, _CYAN, font_thick)


def _draw_servo_overlay(
    frame: np.ndarray,
    angle: float,
    servo_min: float,
    servo_max: float,
) -> None:
    """Draw a servo pan-angle indicator in-place on a BGR frame.

    All pixel dimensions scale with the frame resolution relative to 640×480.
    """
    h, w = frame.shape[:2]
    scale = min(w / 640.0, h / 480.0)
    servo_ctr = (servo_min + servo_max) / 2.0

    # ── Text label (bottom-left) ────────────────────────────────────────
    text = f"Pan: {angle:.0f}\u00b0"
    font = cv2.FONT_HERSHEY_SIMPLEX
    font_scale = max(0.7, 1.1 * scale)
    thickness = max(1, round(2 * scale))
    (tw, th), _ = cv2.getTextSize(text, font, font_scale, thickness)

    pad = max(2, int(4 * scale))
    x0, y0 = int(8 * scale), h - int(8 * scale) - th - pad * 2
    bx1, by1 = max(0, x0 - pad), max(0, y0 - pad)
    bx2, by2 = min(w, x0 + tw + pad), min(h, y0 + th + pad)

    # Darken only the small label background ROI
    roi = frame[by1:by2, bx1:bx2]
    if roi.size > 0:
        roi[:] = roi >> 1
    cv2.putText(frame, text, (x0, y0 + th), font, font_scale, _CYAN, thickness, cv2.LINE_AA)

    # ── Arc compass (bottom-right) ────────────────────────────────────────
    radius = max(15, int(40 * scale))
    off_x = max(radius + 4, int(60 * scale))
    off_y = max(radius + 4, int(55 * scale))
    cx, cy = w - off_x, h - off_y
    half_range = max(1.0, (servo_max - servo_min) / 2.0)
    arc_half_deg = 60  # visual arc spans ±60° regardless of servo range
    arc_thick = max(1, round(2 * scale))

    # Background arc (gray) — U opening upward
    cv2.ellipse(frame, (cx, cy), (radius, radius), 0, 210, 330, (80, 80, 80), arc_thick, cv2.LINE_AA)

    # Normalize position within servo range → -1.0 .. +1.0
    norm = (angle - servo_ctr) / half_range
    norm = max(-1.0, min(1.0, norm))

    # cv2 angle 270° = straight up; sweep left/right by arc_half_deg
    pointer_deg = 270.0 + norm * arc_half_deg
    pointer_rad = math.radians(pointer_deg)
    px = int(cx + radius * math.cos(pointer_rad))
    py = int(cy + radius * math.sin(pointer_rad))

    cv2.line(frame, (cx, cy), (px, py), _CYAN, arc_thick, cv2.LINE_AA)
    dot_r = max(2, round(3 * scale))
    cv2.circle(frame, (cx, cy), dot_r, _CYAN, -1, cv2.LINE_AA)
    # Centre tick (straight up = servo centre position)
    tick_len = max(3, int(8 * scale))
    cv2.line(frame, (cx, cy - radius + tick_len - 2), (cx, cy - radius - 2),
             (120, 120, 120), max(1, round(scale)), cv2.LINE_AA)



class VisionService(Service):
    name = "vision"
    tick_seconds = 0.033   # ~30 fps frame-publish cadence; matches camera framerate

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        camera=None,
        camera_config=None,
        servo_min_deg: float = 135.0,
        servo_max_deg: float = 215.0,
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
        # Servo overlay state — updated by bus callbacks
        self._servo_lock = threading.Lock()
        self._servo_angle: Optional[float] = None
        self._servo_min_deg: float = float(servo_min_deg)
        self._servo_max_deg: float = float(servo_max_deg)
        # Background JPEG encoder — decouples copy+draw+encode from capture tick
        self._encode_queue: queue.Queue = queue.Queue(maxsize=1)
        self._encoder_running: bool = False
        self._encoder_thread: Optional[threading.Thread] = None


    def on_start(self) -> None:
        if self._camera is None:
            from src.vision.camera import Camera, CameraConfig
            cfg = self._camera_config or CameraConfig()
            self._camera = Camera(cfg)
        # Derive tick rate from the camera's configured framerate so the
        # service loop stays in sync when framerate changes at runtime.
        if self._camera_config is not None and self._camera_config.framerate > 0:
            self.tick_seconds = 1.0 / self._camera_config.framerate
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
            self.bus.subscribe("object.enabled_changed", self._on_object_enabled_changed)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_rotation", self._on_set_rotation)
        )
        self._unsubs.append(
            self.bus.subscribe("camera.set_resolution", self._on_set_resolution)
        )
        self._unsubs.append(
            self.bus.subscribe("motion.position", self._on_servo_angle)
        )
        self._unsubs.append(
            self.bus.subscribe("motion.limits_changed", self._on_servo_limits)
        )
        # Spawn background encoder thread
        self._encoder_running = True
        self._encoder_thread = threading.Thread(
            target=self._encoder_loop, daemon=True, name="vision-encoder"
        )
        self._encoder_thread.start()
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

    @property
    def resolution(self) -> tuple:
        if self._camera is not None:
            return self._camera.resolution
        cfg = self._camera_config
        return (cfg.width if cfg else 640, cfg.height if cfg else 480)

    def run_tick(self) -> None:
        if self._camera is None:
            return
        # Camera background thread hasn't deposited the first frame yet —
        # this is a normal startup race, not an error condition.
        if not getattr(self._camera, "is_ready", True):
            return
        try:
            frame = self._camera.capture_frame()
        except Exception:
            log.exception("capture_frame failed")
            self.bus.publish("vision.error", {"reason": "capture_failed"})
            return

        # Apply software rotation so detection receives the correctly-oriented frame.
        with self._rotation_lock:
            rot = self._rotation_deg
        if rot:
            frame = _rotate_frame(frame, rot)

        # Store clean frame for detection consumers and bump the frame index.
        with self._lock:
            self._latest = frame
            self._index += 1
            idx = self._index

        # Publish immediately — PerceptionService and ObjectService start
        # Hailo inference on this frame without waiting for JPEG encoding.
        self.bus.publish(
            "vision.frame_ready",
            {"index": idx, "shape": tuple(frame.shape), "ts": time.time()},
        )

        # Snapshot overlay state and hand off to the encoder thread.
        # Drop the frame if the encoder is still busy (queue full).
        with self._det_lock:
            faces = self._latest_faces
            objects = self._latest_objects
        with self._servo_lock:
            servo_angle = self._servo_angle
            servo_min = self._servo_min_deg
            servo_max = self._servo_max_deg
        try:
            self._encode_queue.put_nowait(
                (frame, faces, objects, servo_angle, servo_min, servo_max, idx)
            )
        except queue.Full:
            pass  # encoder is behind — drop this frame silently

    def _encoder_loop(self) -> None:
        """Background thread: copy frame → draw overlays → JPEG encode → publish.

        Runs independently from the service tick so that GIL-contended cv2
        operations (copy, draw, imencode) don't stall the capture loop.
        Frames dropped when encoder can't keep up are silently discarded;
        the MJPEG stream simply delivers the most recent encoded frame.
        """
        while self._encoder_running:
            try:
                item = self._encode_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            frame, faces, objects, servo_angle, servo_min, servo_max, idx = item

            display = frame.copy()
            _draw_overlays(display, faces, objects)
            if servo_angle is not None:
                _draw_servo_overlay(display, servo_angle, servo_min, servo_max)

            ok, buf = cv2.imencode(".jpg", display, [cv2.IMWRITE_JPEG_QUALITY, 60])
            with self._lock:
                self._latest_jpeg = bytes(buf) if ok else None

            self.bus.publish("vision.jpeg_ready", {"index": idx})

    def on_stop(self) -> None:
        self._encoder_running = False
        if self._encoder_thread is not None:
            self._encoder_thread.join(timeout=2.0)
            self._encoder_thread = None
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

    def _on_object_enabled_changed(self, _topic, payload) -> None:
        """Clear cached object detections immediately when detection is disabled."""
        if not isinstance(payload, dict):
            return
        if not payload.get("enabled", True):
            with self._det_lock:
                self._latest_objects = []

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

    def _on_set_resolution(self, _topic, payload) -> None:
        if not isinstance(payload, dict) or "width" not in payload or "height" not in payload:
            return
        w = int(payload["width"])
        h = int(payload["height"])
        if self._camera is None:
            return
        try:
            self._camera.set_resolution(w, h)
            log.info("Camera 1 resolution changed to %dx%d", w, h)
            self.bus.publish("camera.resolution_changed", {"width": w, "height": h})
        except Exception:
            log.exception("Failed to change camera 1 resolution to %dx%d", w, h)

    def _on_servo_angle(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "angle" in payload:
            with self._servo_lock:
                self._servo_angle = float(payload["angle"])

    def _on_servo_limits(self, _topic, payload) -> None:
        if not isinstance(payload, dict):
            return
        with self._servo_lock:
            if "min_deg" in payload:
                self._servo_min_deg = float(payload["min_deg"])
            if "max_deg" in payload:
                self._servo_max_deg = float(payload["max_deg"])
