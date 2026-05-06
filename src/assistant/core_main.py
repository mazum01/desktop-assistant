"""
Core entry point — motion, AV, and (later) perception/dialog.

Runs in a single process with an in-process MessageBus. Crashes here
do NOT affect the thermal-safety unit.

Run:
    python3 -m src.assistant.core_main

Or via systemd: services/systemd/desktop-assistant-core.service
"""

from __future__ import annotations

import sys

from src.assistant.runner import run_services
from src.core.bus import MessageBus
from src.services.audio_capture_service import AudioCaptureService
from src.services.av_service import AVService
from src.services.clock_service import ClockService
from src.services.face_service import FaceService
from src.services.ipc_bridge import IPCBridge
from src.services.motion_service import MotionService
from src.services.perception_service import PerceptionService
from src.services.telemetry_service import TelemetryService
from src.services.tracking_service import TrackingService
from src.services.vision_service import VisionService

# The thermal service runs in a separate process. Its IPCBridge PUBs on
# this endpoint; we SUBscribe to it from the core IPCBridge and re-emit
# events on our local bus so the CLI sees thermal.* topics in `status`.
_THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"


def main() -> int:
    import yaml
    from pathlib import Path
    _cfg_path = Path(__file__).parents[2] / "config" / "assistant.yaml"
    _cfg = yaml.safe_load(_cfg_path.read_text()) if _cfg_path.exists() else {}
    _clock_enabled = _cfg.get("clock_announcements", {}).get("enabled", True)
    _tracking_enabled = _cfg.get("head_tracking", {}).get("enabled", True)
    _recognition_enabled = _cfg.get("face_recognition", {}).get("enabled", True)
    _greeting_cooldown = float(
        _cfg.get("face_recognition", {}).get("greeting_cooldown_s", 300.0)
    )

    from src.motion.head_tracker import HeadTrackerConfig
    from src.services.perception_service import PerceptionConfig

    _ht_cfg_raw = _cfg.get("head_tracking", {})
    _tracker_cfg = HeadTrackerConfig(
        fov_degrees=float(_ht_cfg_raw.get("fov_degrees", 100.0)),
        spring_k=float(_ht_cfg_raw.get("spring_k", 6.0)),
        damping=float(_ht_cfg_raw.get("damping", 2.5)),
        max_speed_deg_s=float(_ht_cfg_raw.get("max_speed_deg_s", 80.0)),
    )
    _perc_cfg = PerceptionConfig(recognition_enabled=_recognition_enabled)

    bus = MessageBus()
    av = AVService(bus=bus)
    vis = VisionService(bus=bus)
    ipc = IPCBridge(
        bus=bus,
        upstream_endpoints=[_THERMAL_PUB],
    )
    ipc.register_rpc("tts_duration", lambda msg: {
        "ok": True,
        "duration_s": av.tts_duration_rpc(msg.get("text", "")),
    })
    return run_services(
        services=[
            MotionService(bus=bus),
            vis,
            AudioCaptureService(bus=bus),
            av,
            PerceptionService(bus=bus, vision_service=vis, config=_perc_cfg),
            TelemetryService(bus=bus),
            ClockService(bus=bus, enabled=_clock_enabled),
            FaceService(bus=bus, greeting_cooldown_s=_greeting_cooldown),
            TrackingService(bus=bus, config=_tracker_cfg, enabled=_tracking_enabled),
            ipc,
        ],
        unit_name="core",
    )


if __name__ == "__main__":
    sys.exit(main())
