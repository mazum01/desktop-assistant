"""Stereo depth service — refines face depth using cam1 + cam2 disparity.

Subscribes to ``perception.faces`` and, for each detected face, uses
template matching between the cam1 frame and cam2 frame to compute the
horizontal disparity.  The resulting stereo depth replaces or supplements
the face-size depth already embedded in the ``perception.faces`` payload.

Published topics
----------------
vision.face_depth
    Payload: {
        "faces": [
            {
                "face_id": str | None,
                "cx": float, "cy": float,
                "depth_m": float,           # combined best estimate
                "depth_face_size_m": float, # face-size method
                "depth_stereo_m": float | None,  # stereo method (None if unavailable)
                "pos_3d": [X_m, Y_m, Z_m],
                "method": "stereo" | "face_size",
            }
        ],
        "ts": float,
    }
"""

from __future__ import annotations

import logging
import time
import threading
from dataclasses import dataclass, field
from typing import Optional

from src.core.service import Service
from src.core.bus import MessageBus
from src.perception.depth_estimator import (
    StereoFaceMatcher,
    face_size_depth,
    focal_px_from_fov,
    to_3d,
)

log = logging.getLogger(__name__)


@dataclass
class StereoConfig:
    baseline_mm: float = 56.0
    known_face_width_m: float = 0.145
    fov_degrees: float = 100.0
    frame_width: int = 640
    frame_height: int = 480
    min_depth_m: float = 0.25
    max_depth_m: float = 6.0


class StereoService(Service):
    """Refines face depth estimates using stereo template matching."""

    name = "stereo"
    tick_seconds = 999.0  # event-driven, not tick-based

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        cam2_service=None,
        config: Optional[StereoConfig] = None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._cam2_svc = cam2_service
        self._cfg = config or StereoConfig()
        self._unsubs: list = []
        self._focal_px: Optional[float] = None
        self._matcher: Optional[StereoFaceMatcher] = None

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_start(self) -> None:
        focal = focal_px_from_fov(self._cfg.frame_width, self._cfg.fov_degrees)
        self._focal_px = focal
        self._matcher = StereoFaceMatcher(
            focal_px=focal,
            baseline_m=self._cfg.baseline_mm / 1000.0,
            min_depth_m=self._cfg.min_depth_m,
            max_depth_m=self._cfg.max_depth_m,
        )
        self._unsubs.append(
            self.bus.subscribe("perception.faces", self._on_faces)
        )
        log.info(
            "StereoService started — baseline=%.0fmm focal=%.1fpx",
            self._cfg.baseline_mm,
            focal,
        )

    def on_stop(self) -> None:
        for u in self._unsubs:
            try:
                u()
            except Exception:
                pass
        self._unsubs.clear()

    @property
    def hardware_ready(self) -> bool:
        cam2_ok = self._cam2_svc is not None and getattr(
            self._cam2_svc, "hardware_ready", False
        )
        return cam2_ok

    # ── Event handler ────────────────────────────────────────────────────

    def _on_faces(self, _topic, payload: dict) -> None:
        faces = payload.get("faces", [])
        if not faces or self._focal_px is None:
            return

        # Grab both frames — non-blocking
        frame1 = None
        frame2 = None
        if self._vision_svc is not None:
            try:
                frame1 = self._vision_svc.latest_frame()
            except Exception:
                pass
        if self._cam2_svc is not None:
            try:
                frame2 = self._cam2_svc.latest_frame()
            except Exception:
                pass

        out_faces = []
        for f in faces:
            bbox = f.get("bbox")
            centroid = f.get("centroid", [0, 0])
            cx, cy = centroid[0], centroid[1]
            face_size_d = f.get("depth_m")  # already computed by perception_service

            stereo_d: Optional[float] = None
            if bbox is not None and frame1 is not None and frame2 is not None and self._matcher is not None:
                try:
                    stereo_d = self._matcher.estimate(bbox, frame1, frame2)
                except Exception:
                    log.debug("stereo estimate failed", exc_info=True)

            # Pick best estimate
            if stereo_d is not None:
                best_d = stereo_d
                method = "stereo"
            elif face_size_d is not None:
                best_d = face_size_d
                method = "face_size"
            else:
                continue  # no depth available

            x_m, y_m, z_m = to_3d(
                cx, cy, best_d,
                self._focal_px,
                self._cfg.frame_width,
                self._cfg.frame_height,
            )

            out_faces.append(
                {
                    "face_id": f.get("face_id"),
                    "cx": cx,
                    "cy": cy,
                    "depth_m": round(best_d, 3),
                    "depth_face_size_m": round(face_size_d, 3) if face_size_d is not None else None,
                    "depth_stereo_m": round(stereo_d, 3) if stereo_d is not None else None,
                    "pos_3d": [round(x_m, 3), round(y_m, 3), round(z_m, 3)],
                    "method": method,
                }
            )

        if out_faces:
            self.bus.publish(
                "vision.face_depth",
                {"faces": out_faces, "ts": time.time()},
            )

    # ── Tick (unused) ─────────────────────────────────────────────────────

    def run_tick(self) -> None:  # pragma: no cover
        pass
