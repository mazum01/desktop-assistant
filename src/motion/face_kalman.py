"""
1-D Kalman filter for face centroid tracking.

State:        [position (px), velocity (px/s)]
Measurement:  position (px) reported by the face detector each frame

The filter smooths noisy bounding-box centroids and maintains a velocity
estimate so callers can predict where the face will be a short time ahead.
This replaces the simple EMA used previously: Kalman is optimal for
Gaussian noise, gives a velocity estimate, and handles variable dt cleanly.
"""

from __future__ import annotations

import numpy as np


class FaceKalman:
    """Lightweight 1-D Kalman filter for a face X centroid.

    Parameters
    ----------
    r : float
        Measurement noise variance (pixels²).  Higher = trust detector less.
        Typical bounding-box jitter is ±15 px → variance ≈ 225; default 400
        is conservative and gives more smoothing.
    q_pos : float
        Process noise for position (px² / step).  Small — faces don't teleport.
    q_vel : float
        Process noise for velocity (px²/s² / step).  Higher = allow faster
        changes in face velocity; lower = smoother but lags during fast motion.
    """

    def __init__(
        self,
        r: float = 400.0,
        q_pos: float = 1.0,
        q_vel: float = 50.0,
    ) -> None:
        self._r = float(r)
        self._q_pos = float(q_pos)
        self._q_vel = float(q_vel)
        self._x: np.ndarray | None = None  # state [position, velocity]
        self._P: np.ndarray | None = None  # 2×2 covariance matrix

    # ── Public API ────────────────────────────────────────────────────────

    def reset(self) -> None:
        """Discard filter state.  The next ``update()`` seeds from scratch."""
        self._x = None
        self._P = None

    @property
    def initialised(self) -> bool:
        return self._x is not None

    def update(self, measurement: float, dt: float) -> tuple[float, float]:
        """Feed a new centroid measurement; return (smoothed_position, velocity).

        On the very first call after ``reset()``/construction the filter is
        seeded with the measurement directly (velocity initialised to 0).

        Parameters
        ----------
        measurement : float
            Raw face centroid X in pixels from the detector.
        dt : float
            Elapsed seconds since the previous call.

        Returns
        -------
        (position, velocity) : tuple[float, float]
            Kalman-filtered estimates in pixels and pixels/second.
        """
        dt = max(dt, 1e-4)

        if self._x is None:
            self._x = np.array([float(measurement), 0.0])
            self._P = np.eye(2) * 500.0
            return float(measurement), 0.0

        # ── Predict ───────────────────────────────────────────────────────
        F = np.array([[1.0, dt], [0.0, 1.0]])          # constant-velocity model
        Q = np.array([[self._q_pos * dt ** 2, 0.0],
                      [0.0,                   self._q_vel * dt]])
        x_pred = F @ self._x
        P_pred = F @ self._P @ F.T + Q

        # ── Update ────────────────────────────────────────────────────────
        # H = [1, 0] — we observe position only
        innov = float(measurement) - x_pred[0]
        S = float(P_pred[0, 0]) + self._r          # innovation covariance (scalar)
        K = P_pred[:, 0] / S                        # 2-element Kalman gain
        self._x = x_pred + K * innov
        self._P = (np.eye(2) - np.outer(K, [1.0, 0.0])) @ P_pred

        return float(self._x[0]), float(self._x[1])

    def predict(self, t_ahead: float) -> float | None:
        """Return predicted position *t_ahead* seconds from now.

        Returns ``None`` if the filter has not been initialised yet.
        """
        if self._x is None:
            return None
        return float(self._x[0] + self._x[1] * t_ahead)
