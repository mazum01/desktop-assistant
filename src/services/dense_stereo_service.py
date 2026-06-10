"""Dense stereo depth service — per-pixel depth map for everything in view.

Runs OpenCV StereoSGBM on both camera frames at up to ``dense_rate_hz`` Hz,
optionally rectifying with a pre-computed calibration file.  Publishes a
full-frame depth map on the bus.

Subscribed topics
-----------------
None (polls camera frames directly from VisionService / RawCameraService).

Published topics
----------------
vision.depth_map
    Payload: {
        "depth_m":   [[float | null, …], …],   # H×W list, null = invalid pixel
        "width":     int,
        "height":    int,
        "nearest_m": float | null,
        "farthest_m": float | null,
        "mean_m":    float | null,
        "valid_pct": float,                    # % of pixels with valid depth
        "calibrated": bool,
        "method":    "sgbm_calibrated" | "sgbm_uncalibrated",
        "ts":        float,
    }
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np

from src.core.service import Service
from src.core.bus import MessageBus
from src.perception.depth_estimator import DenseStereoMatcher
from src.perception.stereo_rectifier import StereoRectifier

log = logging.getLogger(__name__)

_MIN_INTERVAL_S = 0.1   # hard floor — never run SGBM faster than 10 Hz


@dataclass
class DenseStereoConfig:
    rate_hz: float = 3.0
    proc_width: int = 640
    proc_height: int = 480
    num_disparities: int = 128
    block_size: int = 5
    min_depth_m: float = 0.25
    max_depth_m: float = 6.0
    baseline_mm: float = 56.0
    fov_degrees: float = 100.0
    enabled: bool = False


class DenseStereoService(Service):
    """Per-pixel stereo depth map using StereoSGBM."""

    name = "dense_stereo"
    tick_seconds = 999.0  # event-driven via background thread

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        cam2_service=None,
        config=None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service
        self._cam2_svc = cam2_service
        if isinstance(config, dict):
            d = config.get("depth", config)
            bm = d.get("baseline_m", None)
            self._cfg = DenseStereoConfig(
                rate_hz=d.get("dense_rate_hz", 3.0),
                proc_width=d.get("dense_width", 640),
                proc_height=d.get("dense_height", 480),
                num_disparities=d.get("num_disparities", 128),
                block_size=d.get("block_size", 5),
                min_depth_m=d.get("min_depth_m", 0.25),
                max_depth_m=d.get("max_depth_m", 6.0),
                baseline_mm=bm * 1000.0 if bm is not None else d.get("baseline_mm", 56.0),
                fov_degrees=d.get("fov_degrees", 100.0),
                enabled=bool(d.get("dense_enabled", False)),
            )
        else:
            self._cfg = config or DenseStereoConfig()

        self._enabled: bool = self._cfg.enabled
        self._rectifier: Optional[StereoRectifier] = None
        self._matcher: Optional[DenseStereoMatcher] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._unsubs: list = []

        # Latest depth map cached for API use
        self._latest_payload: Optional[dict] = None
        self._latest_lock = threading.Lock()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_start(self) -> None:
        from src.perception.depth_estimator import focal_px_from_fov

        self._rectifier = StereoRectifier()

        focal_px = focal_px_from_fov(self._cfg.proc_width, self._cfg.fov_degrees)
        Q = self._rectifier.Q if self._rectifier.calibrated else None

        self._matcher = DenseStereoMatcher(
            Q=Q,
            focal_px=focal_px,
            baseline_m=self._cfg.baseline_mm / 1000.0,
            proc_width=self._cfg.proc_width,
            proc_height=self._cfg.proc_height,
            num_disparities=self._cfg.num_disparities,
            block_size=self._cfg.block_size,
            min_depth_m=self._cfg.min_depth_m,
            max_depth_m=self._cfg.max_depth_m,
        )

        if self.bus:
            self._unsubs.append(
                self.bus.subscribe("depth.set_dense_enabled", self._on_set_enabled)
            )

        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop, name="dense-stereo", daemon=True
        )
        self._thread.start()
        log.info(
            "DenseStereoService started — %dx%d @ %.1fHz, calibrated=%s, enabled=%s",
            self._cfg.proc_width, self._cfg.proc_height,
            self._cfg.rate_hz, self._rectifier.calibrated, self._enabled,
        )

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def _on_set_enabled(self, _topic, payload: dict) -> None:
        self._enabled = bool(payload.get("enabled", True))
        log.info("DenseStereoService: enabled=%s", self._enabled)

    @property
    def hardware_ready(self) -> bool:
        cam2_ok = self._cam2_svc is not None and getattr(
            self._cam2_svc, "hardware_ready", False
        )
        return cam2_ok

    def latest_payload(self) -> Optional[dict]:
        """Return the most recent depth map payload (for API use)."""
        with self._latest_lock:
            return self._latest_payload

    # ── Background processing loop ───────────────────────────────────────

    def _run_loop(self) -> None:
        interval = max(_MIN_INTERVAL_S, 1.0 / max(0.1, self._cfg.rate_hz))
        while not self._stop_event.is_set():
            if not self._enabled:
                self._stop_event.wait(timeout=0.5)
                continue
            t0 = time.monotonic()
            try:
                self._process_one()
            except Exception:
                log.debug("DenseStereoService: frame processing failed", exc_info=True)
            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    def _process_one(self) -> None:
        frame1 = frame2 = None
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

        if frame1 is None or frame2 is None:
            return

        self._process_pair(frame1, frame2)

    def _process_pair(self, frame1, frame2) -> None:
        """Process a frame pair and update the cached payload."""
        assert self._rectifier is not None
        assert self._matcher is not None

        rect1, rect2 = self._rectifier.rectify(frame1, frame2)
        depth_m = self._matcher.compute(rect1, rect2)
        stats = self._matcher.summary(depth_m)

        calibrated = self._rectifier.calibrated
        method = "sgbm_calibrated" if calibrated else "sgbm_uncalibrated"

        # Convert NaN → None for JSON serialisation
        depth_list = [
            [None if np.isnan(v) else round(float(v), 3) for v in row]
            for row in depth_m.tolist()
        ]

        payload = {
            "depth_m": depth_list,
            "width": depth_m.shape[1],
            "height": depth_m.shape[0],
            "nearest_m": stats["nearest_m"],
            "farthest_m": stats["farthest_m"],
            "mean_m": stats["mean_m"],
            "valid_pct": stats["valid_pct"],
            "calibrated": calibrated,
            "method": method,
            "ts": time.time(),
        }

        with self._latest_lock:
            self._latest_payload = payload

        if self.bus:
            self.bus.publish("vision.depth_map", payload)

    # ── Tick (unused) ────────────────────────────────────────────────────

    def run_tick(self) -> None:  # pragma: no cover
        pass
