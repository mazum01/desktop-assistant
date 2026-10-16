"""Monocular depth estimation using Hailo-8 / Hailo-10H scdepthv3.

Runs the scdepthv3 HEF on a single camera frame at up to ``mono_rate_hz`` Hz.
Output is a *relative* depth map — values are not in metres but are inversely
proportional to distance (higher value = closer to camera).  The map is
normalised to [0, 1] and published on the bus alongside a best-effort metric
estimate using a configurable scale factor.

Subscribed topics
-----------------
None (polls camera frames directly from VisionService).

Published topics
----------------
vision.mono_depth_map
    Payload: {
        "depth_rel":  [[float, …], …],   # H×W list, normalised [0,1] relative depth
        "depth_m":    [[float|null, …], …],  # H×W metric estimate (null if scale unknown)
        "width":      int,
        "height":     int,
        "nearest_rel": float,   # max relative depth value (closest object)
        "farthest_rel": float,  # min relative depth value (farthest object)
        "scale_factor": float,  # metres-per-unit used for metric conversion
        "hardware_ready": bool,
        "ts":         float,
    }
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np

from src.core.service import Service
from src.core.bus import MessageBus
from src.perception.hailo_models import resolve_model

log = logging.getLogger(__name__)

_DEFAULT_HEF = "auto"
_MODEL_H, _MODEL_W = 256, 320   # fixed input/output resolution for scdepthv3
_MIN_INTERVAL_S = 0.05          # hard floor — never run faster than 20 Hz


@dataclass
class MonoDepthConfig:
    rate_hz: float = 3.0
    hef_path: str = str(_DEFAULT_HEF)
    # scale_factor converts normalised relative depth to approximate metres.
    # This is empirical and scene-dependent; set to None to skip metric estimate.
    scale_factor: Optional[float] = None
    min_depth_m: float = 0.25
    max_depth_m: float = 6.0
    enabled: bool = False


class MonoDepthService(Service):
    """Per-pixel monocular depth map using device-matched scdepthv3."""

    name = "mono_depth"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        vision_service=None,
        config=None,
    ) -> None:
        super().__init__(bus=bus)
        self._vision_svc = vision_service

        if isinstance(config, dict):
            d = config.get("depth", config)
            self._cfg = MonoDepthConfig(
                rate_hz=d.get("mono_rate_hz", 3.0),
                hef_path=d.get("mono_hef_path", str(_DEFAULT_HEF)),
                scale_factor=d.get("mono_scale_factor", None),
                min_depth_m=d.get("min_depth_m", 0.25),
                max_depth_m=d.get("max_depth_m", 6.0),
                enabled=bool(d.get("mono_enabled", False)),
            )
        else:
            self._cfg = config or MonoDepthConfig()

        self._enabled: bool = self._cfg.enabled
        self._engine = None
        self._latest_payload: Optional[dict] = None
        self._latest_lock = threading.Lock()
        self._unsubs: list = []

    # ── Lifecycle ────────────────────────────────────────────────────────

    def on_start(self) -> None:
        from src.perception.hailo_inference import HailoInference
        self._engine = None
        hef = resolve_model("mono_depth", self._cfg.hef_path)
        if hef is None:
            log.warning("MonoDepthService: compatible model missing; mono depth is idle")
            return
        self._engine = HailoInference(hef)
        if not self._engine.hardware_ready:
            log.warning("MonoDepthService: Hailo hardware unavailable or HEF incompatible (%s) — mono depth will remain idle", self._cfg.hef_path)
            return

        if self.bus:
            self._unsubs.append(
                self.bus.subscribe("depth.set_mono_enabled", self._on_set_enabled)
            )
        log.info(
            "MonoDepthService started — %.1f Hz, hardware=%s, enabled=%s",
            self._cfg.rate_hz, self._engine.hardware_ready, self._enabled,
        )

    def on_stop(self) -> None:
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._engine:
            try:
                self._engine.__exit__(None, None, None)
            except Exception:
                pass
        self._engine = None

    def _on_set_enabled(self, _topic, payload: dict) -> None:
        self._enabled = bool(payload.get("enabled", True))
        log.info("MonoDepthService: enabled=%s", self._enabled)

    @property
    def hardware_ready(self) -> bool:
        return self._engine is not None and getattr(self._engine, "hardware_ready", False)

    def latest_payload(self) -> Optional[dict]:
        with self._latest_lock:
            return self._latest_payload

    # ── Background loop ──────────────────────────────────────────────────

    def _run_loop(self) -> None:
        interval = max(_MIN_INTERVAL_S, 1.0 / max(0.1, self._cfg.rate_hz))
        while not self._stop_event.is_set():
            if not self._enabled or not self.hardware_ready:
                self._stop_event.wait(timeout=0.5)
                continue
            t0 = time.monotonic()
            try:
                self._process_one()
            except Exception:
                log.exception("MonoDepthService: frame processing failed")
            elapsed = time.monotonic() - t0
            remaining = interval - elapsed
            if remaining > 0:
                self._stop_event.wait(timeout=remaining)

    def _process_one(self) -> None:
        frame = None
        if self._vision_svc is not None:
            try:
                frame = self._vision_svc.latest_frame()
            except Exception:
                pass
        if frame is None:
            return
        self._process_frame(frame)

    def _process_frame(self, frame: np.ndarray) -> None:
        """Run inference on a single BGR frame and update cached payload."""
        assert self._engine is not None

        # Resize to model input size
        resized = cv2.resize(frame, (_MODEL_W, _MODEL_H), interpolation=cv2.INTER_LINEAR)
        inp = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)

        outputs = self._engine.infer({"input_layer1": inp})
        if not outputs:
            raise RuntimeError("Mono depth inference returned no outputs")
        raw_key = next(iter(outputs))
        raw = outputs[raw_key]  # shape (H, W, 1) or (1, H, W, 1) — float32
        raw = raw.squeeze()     # → (H, W)

        raw_f = raw.astype(np.float32)
        if raw_f.ndim != 2 or not np.isfinite(raw_f).all():
            raise ValueError("Mono depth output must be a finite 2D map")

        # Normalize output to [0, 1] using actual range.
        # scdepthv3 outputs float32 log-inverse depth (larger = closer).
        vmin = float(raw_f.min())
        vmax = float(raw_f.max())
        if vmax > vmin:
            depth_rel = (raw_f - vmin) / (vmax - vmin)
        else:
            depth_rel = np.zeros_like(raw_f)

        nearest_rel = float(depth_rel.max())
        farthest_rel = float(depth_rel.min())

        # Optional metric estimate: depth_m = scale_factor / (rel + eps)
        # scdepthv3 outputs inverse depth, so larger value = closer
        depth_m_list: list = []
        scale = self._cfg.scale_factor
        if scale is not None:
            eps = 1e-6
            depth_m = scale / (depth_rel + eps)
            depth_m = np.clip(depth_m, self._cfg.min_depth_m, self._cfg.max_depth_m)
            depth_m_list = [
                [round(float(v), 3) for v in row]
                for row in depth_m.tolist()
            ]
        else:
            depth_m_list = []

        payload = {
            "depth_rel": [
                [round(float(v), 4) for v in row]
                for row in depth_rel.tolist()
            ],
            "depth_m": depth_m_list,
            "width": _MODEL_W,
            "height": _MODEL_H,
            "nearest_rel": nearest_rel,
            "farthest_rel": farthest_rel,
            "scale_factor": scale,
            "hardware_ready": self.hardware_ready,
            "ts": time.time(),
        }

        with self._latest_lock:
            self._latest_payload = payload

        if self.bus:
            self.bus.publish("vision.mono_depth_map", payload)

    def run_tick(self) -> None:  # pragma: no cover
        pass
