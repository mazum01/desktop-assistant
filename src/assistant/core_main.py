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
from src.services.ipc_bridge import IPCBridge
from src.services.motion_service import MotionService
from src.services.perception_service import PerceptionService
from src.services.telemetry_service import TelemetryService
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
            PerceptionService(bus=bus, vision_service=vis),
            TelemetryService(bus=bus),
            ClockService(bus=bus, enabled=_clock_enabled),
            ipc,  # last so all earlier services emit
                  # service.started events on the wire
        ],
        unit_name="core",
    )


if __name__ == "__main__":
    sys.exit(main())
