"""
Hailo accelerator die-temperature reader.

Reads the on-chip thermal sensors through the HailoRT control channel. The
control path works while another process (perception) owns the inference
VDevice, so this never interferes with running models. The device handle is
opened and released on every read so a restarting perception process can
always re-acquire the accelerator.

Monitoring only — readings never drive fan control.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger(__name__)

_THERMAL_CONFIG = Path(__file__).resolve().parent.parent.parent / "config" / "thermal.yaml"


@dataclass(frozen=True)
class HailoMonitorConfig:
    enabled: bool = True
    poll_interval_s: float = 5.0

    @classmethod
    def from_yaml(cls, path: Path = _THERMAL_CONFIG) -> "HailoMonitorConfig":
        try:
            import yaml

            with open(path) as f:
                section = (yaml.safe_load(f) or {}).get("hailo", {}) or {}
            return cls(
                enabled=bool(section.get("enabled", True)),
                poll_interval_s=max(1.0, float(section.get("poll_interval_s", 5.0))),
            )
        except Exception as exc:
            log.warning("Could not load Hailo monitor config from %s (%s) — using defaults", path, exc)
            return cls()


@dataclass(frozen=True)
class HailoTemperature:
    celsius: float
    ts0_celsius: float
    ts1_celsius: float


def _default_device_factory():
    import hailo_platform as hp

    return hp.Device()


class HailoTemperatureReader:
    """Reads the Hailo die temperature; returns ``None`` when unavailable."""

    def __init__(self, device_factory: Optional[Callable[[], object]] = None) -> None:
        self._device_factory = device_factory or _default_device_factory
        self._failure_logged = False

    def read(self) -> Optional[HailoTemperature]:
        device = None
        try:
            device = self._device_factory()
            sample = device.control.get_chip_temperature()
            ts0 = float(sample.ts0_temperature)
            ts1 = float(sample.ts1_temperature)
        except Exception as exc:
            if not self._failure_logged:
                log.warning("Hailo temperature unavailable: %s", exc)
                self._failure_logged = True
            return None
        finally:
            if device is not None:
                try:
                    device.release()
                except Exception:
                    log.debug("Hailo device release failed", exc_info=True)
        if self._failure_logged:
            log.info("Hailo temperature readings restored")
            self._failure_logged = False
        return HailoTemperature(celsius=max(ts0, ts1), ts0_celsius=ts0, ts1_celsius=ts1)
