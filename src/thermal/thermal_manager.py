"""
Thermal manager — reads TMP117, reads Pi CPU die temp, drives fan PWM
via a configurable piecewise-linear control curve.

Temperature input is a weighted blend of:
  - TMP117 (case/ambient, I²C)
  - Pi SoC die temp (/sys/class/thermal/thermal_zone0/temp)
Default blend: 20% case + 80% CPU.

Control curve: arbitrary (temp_c, duty%) pairs, piecewise-linear.
Failsafe: sensor error — fan at 100%.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .tmp117 import TMP117, TMP117Error
from .fan import FanController
from .fan_tach import FanTach

log = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_THERMAL_CONFIG = _PROJECT_ROOT / "config" / "thermal.yaml"
_CPU_TEMP_PATH = Path("/sys/class/thermal/thermal_zone0/temp")


@dataclass
class ThermalThresholds:
    safe_max_c: float    = 50.0
    warn_max_c: float    = 65.0
    critical_c: float    = 75.0
    fan_min_duty: float  = 30.0
    fan_max_duty: float  = 100.0
    control_points: list[tuple[float, float]] | None = None
    poll_interval_s: float = 1.0
    tach_enabled: bool   = True
    case_weight: float   = 0.2
    cpu_weight: float    = 0.8
    spin_up_duty: float  = 60.0   # kick duty when starting fan from rest
    spin_up_duration_s: float = 3.0  # how long to hold kick duty

    @classmethod
    def from_yaml(cls, path: Path = _THERMAL_CONFIG) -> "ThermalThresholds":
        """Load thresholds from thermal.yaml, falling back to defaults on error."""
        try:
            import yaml  # type: ignore
            with open(path) as f:
                cfg = yaml.safe_load(f) or {}
            t = cfg.get("thresholds", {})
            tach_enabled = bool(cfg.get("tach", {}).get("enabled", True))
            blend = cfg.get("temp_blend", {})
            case_w = float(blend.get("case_weight", 0.2))
            cpu_w  = float(blend.get("cpu_weight",  0.8))
            return cls(
                safe_max_c=float(t.get("safe_max_c", 50.0)),
                warn_max_c=float(t.get("warn_max_c", 65.0)),
                critical_c=float(t.get("critical_c", 75.0)),
                fan_min_duty=float(t.get("fan_min_duty", 30.0)),
                fan_max_duty=float(t.get("fan_max_duty", 100.0)),
                control_points=_parse_control_points(t.get("control_points")),
                poll_interval_s=float(t.get("poll_interval_s", 1.0)),
                tach_enabled=tach_enabled,
                case_weight=case_w,
                cpu_weight=cpu_w,
                spin_up_duty=float(t.get("spin_up_duty", 60.0)),
                spin_up_duration_s=float(t.get("spin_up_duration_s", 3.0)),
            )
        except Exception as exc:
            log.warning("Could not load %s (%s) — using defaults", path, exc)
            return cls()


class ThermalManager:
    """
    Runs a background thread that polls the TMP117 and adjusts fan speed.

    Usage:
        mgr = ThermalManager()
        mgr.start()
        ...
        mgr.stop()
    """

    def __init__(
        self,
        thresholds: Optional[ThermalThresholds] = None,
        on_critical: Optional[Callable[[float], None]] = None,
        i2c_bus: int = 1,
        gpio_pin: int = 13,
        tach_gpio: int = 6,
        tach_pulses_per_rev: int = 2,
    ) -> None:
        self._thresh = thresholds or ThermalThresholds.from_yaml()
        self._on_critical = on_critical
        self._sensor = TMP117(bus=i2c_bus)
        self._fan = FanController(gpio_pin=gpio_pin)
        if self._thresh.tach_enabled:
            self._tach: Optional[FanTach] = FanTach(gpio=tach_gpio, pulses_per_rev=tach_pulses_per_rev)
        else:
            self._tach = None
            log.info("FanTach disabled via config")
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._last_case_c: Optional[float] = None    # TMP117 reading
        self._last_cpu_c: Optional[float] = None     # Pi SoC die reading
        self._last_blended_c: Optional[float] = None # weighted average used for control
        self._case_weight: float = self._thresh.case_weight
        self._cpu_weight:  float = self._thresh.cpu_weight
        self._sensor_ok: bool = True
        self._override_duty: Optional[float] = None
        self._control_points: list[tuple[float, float]] = _normalise_control_points(
            self._thresh.control_points or _legacy_control_points(self._thresh)
        )
        self._lock = threading.Lock()
        self._prev_computed_duty: float = 0.0  # tracks last thermal-computed duty for kick-start
        self._kick_until: float = 0.0          # monotonic time until kick-start expires

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the thermal control loop in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="thermal-mgr")
        self._thread.start()
        log.info("ThermalManager started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._fan.close()
        if self._tach is not None:
            self._tach.close()
        self._sensor.close()
        log.info("ThermalManager stopped")

    @property
    def temperature_c(self) -> Optional[float]:
        """Blended temperature used for fan control."""
        return self._last_blended_c

    @property
    def case_temp_c(self) -> Optional[float]:
        return self._last_case_c

    @property
    def cpu_temp_c(self) -> Optional[float]:
        return self._last_cpu_c

    @property
    def case_weight(self) -> float:
        return self._case_weight

    @property
    def cpu_weight(self) -> float:
        return self._cpu_weight

    @property
    def fan_duty(self) -> float:
        return self._fan.duty

    @property
    def fan_rpm(self) -> Optional[int]:
        return self._tach.rpm if self._tach is not None else None

    @property
    def tach_enabled(self) -> bool:
        return self._tach is not None

    @property
    def fan_backend(self) -> str:
        return self._fan.backend

    @property
    def sensor_ok(self) -> bool:
        return self._sensor_ok

    @property
    def override_active(self) -> bool:
        return self._override_duty is not None

    @property
    def override_duty(self) -> Optional[float]:
        return self._override_duty

    def set_override(self, duty_percent: float) -> None:
        """Pin fan at a fixed duty, bypassing the thermal loop."""
        duty_percent = max(0.0, min(100.0, duty_percent))
        self._override_duty = duty_percent
        self._fan.set_duty(duty_percent)
        log.info("Fan override set to %.1f%%", duty_percent)

    def clear_override(self) -> None:
        """Return fan control to the thermal loop."""
        self._override_duty = None
        log.info("Fan override cleared — returning to thermal auto mode")

    def get_control_points(self) -> list[dict[str, float]]:
        with self._lock:
            return [{"temp_c": t, "duty": d} for t, d in self._control_points]

    def set_control_points(self, points: list[tuple[float, float]]) -> None:
        with self._lock:
            self._control_points = _normalise_control_points(points)
        log.info("Fan control points updated: %s", self._control_points)

    def get_temp_blend(self) -> dict[str, float]:
        return {"case_weight": self._case_weight, "cpu_weight": self._cpu_weight}

    def set_temp_blend(self, case_weight: float, cpu_weight: float) -> None:
        total = case_weight + cpu_weight
        if total <= 0:
            raise ValueError("Weights must sum to a positive number")
        self._case_weight = case_weight / total
        self._cpu_weight  = cpu_weight  / total
        log.info("Temp blend updated: case=%.0f%% cpu=%.0f%%",
                 self._case_weight * 100, self._cpu_weight * 100)

    # ------------------------------------------------------------------
    # Internal control loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                case_c = self._sensor.read_temperature_c()
                self._last_case_c = case_c
                self._sensor_ok = True
            except TMP117Error:
                if self._sensor_ok:
                    log.error("TMP117 read failed — engaging thermal fail-safe (fan 100%%)")
                self._sensor_ok = False
                self._fan.set_duty(100.0)
                self._stop_event.wait(timeout=self._thresh.poll_interval_s)
                continue

            cpu_c = _read_cpu_temp_c()
            self._last_cpu_c = cpu_c

            if cpu_c is not None:
                blended = self._case_weight * case_c + self._cpu_weight * cpu_c
            else:
                blended = case_c
            self._last_blended_c = blended

            if self._override_duty is None:
                with self._lock:
                    duty = self._compute_duty(blended)
                # Kick-start: when transitioning from stopped to running, briefly
                # apply a higher duty so the fan can start from rest.
                now = time.monotonic()
                if self._prev_computed_duty == 0.0 and duty > 0.0:
                    self._kick_until = now + self._thresh.spin_up_duration_s
                    log.info(
                        "Fan kick-start: %.1f°C → %.1f%% duty (kicking to %.1f%% for %.0fs)",
                        blended, duty, self._thresh.spin_up_duty, self._thresh.spin_up_duration_s,
                    )
                effective_duty = (
                    max(duty, self._thresh.spin_up_duty)
                    if now < self._kick_until else duty
                )
                self._prev_computed_duty = duty
                self._fan.set_duty(effective_duty)
            else:
                self._fan.set_duty(self._override_duty)

            self._check_critical(blended)
            self._stop_event.wait(timeout=self._thresh.poll_interval_s)

    def _compute_duty(self, temp_c: float) -> float:
        pts = self._control_points
        if temp_c <= pts[0][0]:
            return pts[0][1]
        if temp_c >= pts[-1][0]:
            return pts[-1][1]
        for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
            if t0 <= temp_c <= t1:
                if t1 <= t0:
                    return d1
                ratio = (temp_c - t0) / (t1 - t0)
                return d0 + ratio * (d1 - d0)
        return pts[-1][1]

    def _check_critical(self, temp_c: float) -> None:
        if temp_c >= self._thresh.critical_c:
            log.warning("CRITICAL temperature: %.2f °C", temp_c)
            if self._on_critical:
                self._on_critical(temp_c)


def _read_cpu_temp_c() -> Optional[float]:
    """Read Pi SoC die temperature from sysfs. Returns None if unavailable."""
    try:
        return int(_CPU_TEMP_PATH.read_text().strip()) / 1000.0
    except Exception:
        return None


def _parse_control_points(raw) -> list[tuple[float, float]] | None:
    if not isinstance(raw, list) or len(raw) < 2:
        return None
    points: list[tuple[float, float]] = []
    for item in raw:
        if isinstance(item, dict) and "temp_c" in item and "duty" in item:
            points.append((float(item["temp_c"]), float(item["duty"])))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            points.append((float(item[0]), float(item[1])))
    return points if len(points) >= 2 else None


def _legacy_control_points(t: ThermalThresholds) -> list[tuple[float, float]]:
    if t.critical_c <= t.safe_max_c:
        return [(t.safe_max_c, t.fan_min_duty), (t.safe_max_c + 1.0, t.fan_max_duty)]
    warn_ratio = (t.warn_max_c - t.safe_max_c) / (t.critical_c - t.safe_max_c)
    warn_ratio = max(0.0, min(1.0, warn_ratio))
    warn_duty = t.fan_min_duty + warn_ratio * (t.fan_max_duty - t.fan_min_duty)
    return [
        (t.safe_max_c, t.fan_min_duty),
        (t.warn_max_c, warn_duty),
        (t.critical_c, t.fan_max_duty),
    ]


def _normalise_control_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    cleaned: list[tuple[float, float]] = []
    for temp_c, duty in points:
        cleaned.append((float(temp_c), max(0.0, min(100.0, float(duty)))))
    cleaned.sort(key=lambda p: p[0])
    dedup: dict[float, float] = {}
    for temp_c, duty in cleaned:
        dedup[temp_c] = duty
    out = sorted(dedup.items(), key=lambda p: p[0])
    if len(out) < 2:
        raise ValueError("Need at least two control points")
    return out
