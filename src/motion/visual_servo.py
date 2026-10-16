"""Fresh-observation visual servoing and bounded, repeatable plant identification."""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np


@dataclass
class VisualServoConfig:
    max_age_s: float = 1.0
    acquire_frames: int = 3
    association_radius: float = 0.12
    stop_error: float = 0.02
    restart_error: float = 0.035
    gain: float = 0.35
    max_step_deg: float = 2.0
    max_speed_deg_s: float = 12.0
    max_accel_deg_s2: float = 30.0
    settle_s: float = 0.6
    calibration_step_deg: float = 2.0
    calibration_samples: int = 4
    calibration_timeout_s: float = 20.0
    validation_timeout_s: float = 90.0
    calibration_path: str = "config/tracking_calibration.json"

    def __post_init__(self):
        numeric = [v for v in asdict(self).values() if isinstance(v, (int, float))]
        if not all(math.isfinite(v) and v > 0 for v in numeric):
            raise ValueError("Visual servo settings must be finite and positive")
        if not self.stop_error < self.restart_error < self.association_radius < 0.5:
            raise ValueError("Require stop < restart < association radius < 0.5")
        if not 0 < self.gain <= 0.5 or self.max_step_deg > 5:
            raise ValueError("Visual servo gain/step exceeds conservative safety bounds")
        if self.max_speed_deg_s > 30 or self.max_accel_deg_s2 > 90:
            raise ValueError("Visual servo speed/acceleration exceeds safety bounds")
        if not 0 < self.calibration_step_deg <= 3:
            raise ValueError("Calibration steps must be within 0-3 degrees")
        if (type(self.acquire_frames) is not int or self.acquire_frames < 2
                or type(self.calibration_samples) is not int or self.calibration_samples < 3):
            raise ValueError("Acquisition requires >=2 frames; calibration requires >=3")


class FaceLock:
    """Associate one target; never switch directly to a distant detection."""

    def __init__(self, cfg: VisualServoConfig):
        self.cfg = cfg
        self.center: np.ndarray | None = None
        self.identity: str | None = None
        self.hits = 0
        self.last_seen = -math.inf
        self.last_stamp = -math.inf

    def reset(self):
        self.center = None
        self.identity = None
        self.hits = 0
        self.last_seen = -math.inf

    def select(self, faces: list, width: int, height: int, stamp: float, now: float):
        if not math.isfinite(stamp) or stamp <= self.last_stamp:
            return None
        if not 0 <= now - stamp <= self.cfg.max_age_s:
            return None
        self.last_stamp = stamp
        if now - self.last_seen > self.cfg.max_age_s:
            self.reset()
        candidates = []
        for face in faces:
            try:
                point = np.asarray(face["centroid"], dtype=float)
                confidence = float(face.get("confidence", 0))
                bbox = np.asarray(face["bbox"], dtype=float)
                if (point.shape != (2,) or bbox.shape != (4,)
                        or not np.isfinite(point).all() or not np.isfinite(bbox).all()
                        or not 0.5 <= confidence <= 1
                        or not 0 <= point[0] <= width or not 0 <= point[1] <= height
                        or not 0 <= bbox[0] < bbox[2] <= width
                        or not 0 <= bbox[1] < bbox[3] <= height):
                    continue
                candidates.append((face, point / [width, height] - 0.5))
            except (KeyError, TypeError, ValueError):
                continue
        # Saturated, unidentified multi-face results are not a reliable target.
        if len(candidates) > 1 and all(
            f.get("confidence") == 1 and not f.get("face_id") for f, _ in candidates
        ):
            return None
        if self.center is None:
            if len(candidates) != 1:
                return None
            face, center = candidates[0]
        else:
            matches = [
                (f, p) for f, p in candidates
                if np.linalg.norm(p - self.center) <= self.cfg.association_radius
                and not (self.identity and f.get("face_id") and f["face_id"] != self.identity)
            ]
            if not matches:
                self.hits = 0
                return None
            face, center = min(matches, key=lambda item: np.linalg.norm(item[1] - self.center))
        self.center = center
        self.identity = face.get("face_id") or self.identity
        self.last_seen = now
        self.hits += 1
        return (face, center) if self.hits >= self.cfg.acquire_frames else None


@dataclass
class PlantModel:
    jacobian: list[list[float]]
    settle_s: float
    noise: float
    signature: dict

    def matrix(self) -> np.ndarray:
        matrix = np.asarray(self.jacobian, dtype=float)
        if (matrix.shape not in ((2, 1), (2, 2)) or not np.isfinite(matrix).all()
                or not math.isfinite(self.settle_s) or not 0.1 <= self.settle_s <= 10
                or not math.isfinite(self.noise) or not 0 <= self.noise <= 0.03
                or np.linalg.cond(matrix) > 20
                or np.linalg.svd(matrix, compute_uv=False).min() < 0.001
                or (matrix.shape == (2, 1) and abs(matrix[0, 0]) < 0.001)):
            raise ValueError("Invalid or poorly observable visual plant model")
        return matrix

    def save(self, path: Path) -> None:
        self.matrix()
        fd, name = tempfile.mkstemp(prefix=path.name, dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(asdict(self), stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @classmethod
    def load(cls, path: Path, signature: dict):
        model = cls(**json.loads(path.read_text()))
        model.matrix()
        if model.signature != signature:
            raise ValueError("Calibration does not match camera geometry or servo configuration")
        return model


def identify_plant(commands: np.ndarray, points: np.ndarray, durations: list[float],
                   noise: float, signature: dict) -> PlantModel:
    """Fit repeated +/- probes, validate held-out response, then fit all samples."""
    commands = np.asarray(commands, dtype=float)
    points = np.asarray(points, dtype=float)
    if (commands.ndim != 2 or commands.shape[1] not in (1, 2)
            or points.shape != (len(commands), 2)
            or len(commands) < 5 * commands.shape[1] + 1
            or not np.isfinite(commands).all() or not np.isfinite(points).all()
            or not durations or not all(math.isfinite(t) and 0 < t <= 10 for t in durations)
            or not math.isfinite(noise) or not 0 <= noise <= 0.01):
        raise ValueError("Insufficient, noisy, or invalid calibration observations")
    design = np.column_stack([commands, np.ones(len(commands))])
    split = len(commands) - 2
    train = design[:split]
    if np.linalg.matrix_rank(train) != design.shape[1]:
        raise ValueError("Calibration did not excite every available axis")
    fit = np.linalg.lstsq(train, points[:split], rcond=None)[0]
    tolerance = max(0.006, noise * 3)
    if np.max(np.abs(design[split:] @ fit - points[split:])) > tolerance:
        raise ValueError("Held-out calibration response is inconsistent; keep still and retry")
    fit = np.linalg.lstsq(design, points, rcond=None)[0]
    residual = np.max(np.abs(design @ fit - points))
    if residual > tolerance:
        raise ValueError("Calibration responses are not repeatable; model not saved")
    model = PlantModel(
        fit[:-1].T.tolist(), max(0.6, float(np.percentile(durations, 90))),
        max(noise, float(residual)), signature,
    )
    model.matrix()
    return model


class VisualController:
    """One bounded correction per new, post-settling camera observation."""

    def __init__(self, cfg: VisualServoConfig):
        self.cfg = cfg
        self.last_stamp = -math.inf
        self.ready_at = -math.inf
        self.holding = False
        self.state = "acquiring"

    def correction(self, error, position, limits, stamp, now, model: PlantModel):
        if not math.isfinite(stamp) or stamp <= self.last_stamp:
            return None
        if not 0 <= now - stamp <= self.cfg.max_age_s:
            self.state = "waiting_for_fresh_frame"
            return None
        self.last_stamp = stamp
        if stamp < self.ready_at:
            self.state = "waiting_for_fresh_frame"
            return None
        matrix = model.matrix()
        error = np.asarray(error, dtype=float)
        position = np.asarray(position, dtype=float)
        limits = np.asarray(limits, dtype=float)
        axes = matrix.shape[1]
        if (error.shape != (2,) or position.shape != (axes,)
                or limits.shape != (axes, 2) or not np.isfinite(error).all()
                or not np.isfinite(position).all() or not np.isfinite(limits).all()
                or np.any(limits[:, 0] >= limits[:, 1])):
            raise ValueError("Invalid visual feedback or servo limits")
        stop = max(self.cfg.stop_error, 3 * model.noise)
        restart = max(self.cfg.restart_error, stop * 1.5)
        controlled_error = error[:axes] if axes == 1 else error
        if np.max(np.abs(controlled_error)) <= (restart if self.holding else stop):
            self.holding = True
            self.state = "centered"
            return None
        self.holding = False
        # Damped least squares supports modest cross-axis coupling without
        # amplifying a noisy or nearly singular inverse.
        if axes == 1:
            matrix = matrix[:1]
            error = error[:1]
        damping = np.linalg.svd(matrix, compute_uv=False).min() * 0.1
        delta = -np.linalg.solve(
            matrix.T @ matrix + damping ** 2 * np.eye(axes), matrix.T @ error
        ) * self.cfg.gain
        delta *= min(1.0, self.cfg.max_step_deg / max(np.max(np.abs(delta)), 1e-9))
        target = np.clip(position + delta, limits[:, 0], limits[:, 1])
        distance = float(np.max(np.abs(target - position)))
        if distance < 0.05:
            self.state = "at_limit"
            return None
        # Quintic smoothstep has peak velocity 1.875*d/T and acceleration
        # <5.78*d/T^2. MotionService executes this profile at 50 Hz.
        duration = max(
            model.settle_s, self.cfg.settle_s,
            1.875 * distance / self.cfg.max_speed_deg_s,
            math.sqrt(5.78 * distance / self.cfg.max_accel_deg_s2),
        )
        self.ready_at = now + duration + model.settle_s
        self.state = "correcting"
        return target, duration
