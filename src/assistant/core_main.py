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
from src.services.music_service import MusicService
from src.services.object_service import ObjectService, ObjectConfig
from src.services.perception_service import PerceptionService
from src.services.raw_camera_service import RawCameraService, RawCameraConfig
from src.services.telemetry_service import TelemetryService
from src.services.skills_service import SkillsService
from src.services.tracking_service import TrackingService
from src.services.vision_service import VisionService
from src.core.quiet_hours import QuietHours
from src.services.web_service import WebService
from src.core.runtime_state import load as _load_runtime, save as _save_runtime
from src.services.notification_service import NotificationService

# The thermal service runs in a separate process. Its IPCBridge PUBs on
# this endpoint; we SUBscribe to it from the core IPCBridge and re-emit
# events on our local bus so the CLI sees thermal.* topics in `status`.
_THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"


def main() -> int:
    import yaml
    from pathlib import Path
    _cfg_path = Path(__file__).parents[2] / "config" / "assistant.yaml"
    _cfg = yaml.safe_load(_cfg_path.read_text()) if _cfg_path.exists() else {}

    # Runtime state overlays the base config for toggle settings.
    _rt = _load_runtime()

    _clock_enabled = _cfg.get("clock_announcements", {}).get("enabled", True)
    _tracking_enabled = _cfg.get("head_tracking", {}).get("enabled", True)
    _face_tracking_enabled = _rt.get("head_tracking", {}).get(
        "face_tracking_enabled",
        _cfg.get("head_tracking", {}).get("face_tracking_enabled", True),
    )
    _random_motion_enabled = _rt.get("head_tracking", {}).get(
        "random_motion_enabled",
        _cfg.get("head_tracking", {}).get("random_motion_enabled", True),
    )
    _person_seek_enabled = _cfg.get("head_tracking", {}).get("person_seek_enabled", True)
    _recognition_enabled = _cfg.get("face_recognition", {}).get("enabled", True)
    _fr_cfg = _cfg.get("face_recognition", {})
    _greeting_cooldown_min = float(_fr_cfg.get("greeting_cooldown_min", 30.0))
    _greeting_jitter_pct   = float(_fr_cfg.get("greeting_cooldown_jitter_pct", 25.0))
    _min_absence_s         = float(_fr_cfg.get("min_absence_s", 30.0))
    _confidence_threshold  = float(_fr_cfg.get("confidence_threshold", 0.5))
    _servo_enabled = _rt.get("servo", {}).get(
        "enabled",
        _cfg.get("servo", {}).get("enabled", True),
    )
    _servo_cfg = _cfg.get("servo", {})
    _soft_min_deg = float(_rt.get("servo", {}).get(
        "soft_min_deg", _servo_cfg.get("soft_min_deg", 135.0)
    ))
    _soft_max_deg = float(_rt.get("servo", {}).get(
        "soft_max_deg", _servo_cfg.get("soft_max_deg", 215.0)
    ))

    from src.motion.head_tracker import HeadTrackerConfig
    from src.services.perception_service import PerceptionConfig

    # Camera config must be built first so we can derive the effective
    # frame width (which changes when rotation is 90° or 270°).
    _cam_cfg_raw = _cfg.get("camera", {})
    _cam2_cfg_raw_early = _cfg.get("camera2", {})
    from src.vision.camera import CameraConfig as _CameraConfig
    _camera_rotation_deg = int(_rt.get("camera", {}).get(
        "rotation_deg", _cam_cfg_raw.get("rotation_deg", 0)
    )) % 360
    _camera2_rotation_deg = int(_rt.get("camera2", {}).get(
        "rotation_deg", _cam2_cfg_raw_early.get("rotation_deg", 0)
    )) % 360
    _cam_width  = int(_rt.get("camera", {}).get("width",  _cam_cfg_raw.get("width", 640)))
    _cam_height = int(_rt.get("camera", {}).get("height", _cam_cfg_raw.get("height", 480)))
    _camera_cfg = _CameraConfig(
        width=_cam_width,
        height=_cam_height,
        framerate=int(_cam_cfg_raw.get("framerate", 30)),
        rotation_deg=_camera_rotation_deg,
        af_mode=str(_cam_cfg_raw.get("af_mode", "continuous")),
        lens_position=float(_cam_cfg_raw.get("lens_position", 0.0)),
    )

    def _tracking_frame_width(cam_w: int, cam_h: int, rot_deg: int) -> int:
        """Return the horizontal pixel count of the frame as seen by detection.

        For 90° / 270° rotations cv2 swaps width ↔ height, so the axis the
        servo tracks (left-right) maps to the original camera height.
        """
        return cam_h if rot_deg in (90, 270) else cam_w

    _ht_cfg_raw = _cfg.get("head_tracking", {})
    _speaking_motion_cfg = _ht_cfg_raw.get("speaking_motion", {})
    _tracker_cfg = HeadTrackerConfig(
        frame_width=_tracking_frame_width(_cam_width, _cam_height, _camera_rotation_deg),
        fov_degrees=float(_ht_cfg_raw.get("fov_degrees", 100.0)),
        spring_k=float(_ht_cfg_raw.get("spring_k", 2.0)),
        damping=float(_ht_cfg_raw.get("damping", 3.2)),
        max_speed_deg_s=float(_ht_cfg_raw.get("max_speed_deg_s", 60.0)),
        tracking_gain=float(_ht_cfg_raw.get("tracking_gain", 0.35)),
        face_ema_alpha=float(_ht_cfg_raw.get("face_ema_alpha", 0.25)),
        invert_pan=bool(_ht_cfg_raw.get("invert_pan", False)),
    )
    _perc_cfg = PerceptionConfig(recognition_enabled=_recognition_enabled)

    _obj_cfg_raw = _cfg.get("object_detection", {})
    _obj_cfg = ObjectConfig(
        enabled=bool(_obj_cfg_raw.get("enabled", True)),
        max_fps=float(_obj_cfg_raw.get("max_fps", 2.0)),
        conf_threshold=float(_obj_cfg_raw.get("conf_threshold", 0.40)),
        max_objects=int(_obj_cfg_raw.get("max_objects", 8)),
    )

    _notif_cfg = _cfg.get("notifications", {})
    _notif_thermal_cfg = _notif_cfg.get("thermal_alerts", {})
    _notif_absence_cfg = _notif_cfg.get("absence_alerts", {})

    _web_cfg = _cfg.get("web_dashboard", {})
    _web_enabled = _web_cfg.get("enabled", True)
    _web_port = int(_web_cfg.get("port", 8080))
    _web_host = _web_cfg.get("host", "0.0.0.0")

    _qh = QuietHours.from_config(
        cfg_dir=_cfg_path.parent,
        yaml_defaults=_cfg.get("quiet_hours", {}),
    )

    bus = MessageBus()

    # Persist toggle state changes so they survive daemon restarts.
    _rt_state: dict = {
        "servo": {
            "enabled": _servo_enabled,
            "soft_min_deg": _soft_min_deg,
            "soft_max_deg": _soft_max_deg,
        },
        "head_tracking": {
            "face_tracking_enabled": _face_tracking_enabled,
            "random_motion_enabled": _random_motion_enabled,
        },
        "camera": {
            "rotation_deg": _camera_rotation_deg,
            "width": _cam_width,
            "height": _cam_height,
        },
        "camera2": {
            "rotation_deg": _camera2_rotation_deg,
        },
    }

    def _on_servo_changed(_t, payload):
        if isinstance(payload, dict) and "enabled" in payload:
            _rt_state["servo"]["enabled"] = bool(payload["enabled"])
            _save_runtime(_rt_state)

    def _on_limits_changed(_t, payload):
        if isinstance(payload, dict):
            if "min_deg" in payload:
                _rt_state["servo"]["soft_min_deg"] = float(payload["min_deg"])
            if "max_deg" in payload:
                _rt_state["servo"]["soft_max_deg"] = float(payload["max_deg"])
            _save_runtime(_rt_state)

    def _on_face_tracking_changed(_t, payload):
        if isinstance(payload, dict) and "enabled" in payload:
            _rt_state["head_tracking"]["face_tracking_enabled"] = bool(payload["enabled"])
            _save_runtime(_rt_state)

    def _on_random_motion_changed(_t, payload):
        if isinstance(payload, dict) and "enabled" in payload:
            _rt_state["head_tracking"]["random_motion_enabled"] = bool(payload["enabled"])
            _save_runtime(_rt_state)

    def _on_camera_rotation_changed(_t, payload):
        if isinstance(payload, dict) and "rotation_deg" in payload:
            new_rot = int(payload["rotation_deg"]) % 360
            _rt_state["camera"]["rotation_deg"] = new_rot
            _save_runtime(_rt_state)
            # Keep tracker frame_width in sync so tracking stays accurate
            # when the user changes camera rotation at runtime.
            new_fw = _tracking_frame_width(_cam_width, _cam_height, new_rot)
            if tracking_svc is not None:
                tracking_svc.update_frame_width(new_fw)

    def _on_camera2_rotation_changed(_t, payload):
        if isinstance(payload, dict) and "rotation_deg" in payload:
            _rt_state["camera2"]["rotation_deg"] = int(payload["rotation_deg"]) % 360
            _save_runtime(_rt_state)

    def _on_camera_resolution_changed(_t, payload):
        if isinstance(payload, dict) and "width" in payload and "height" in payload:
            new_w = int(payload["width"])
            new_h = int(payload["height"])
            _rt_state["camera"]["width"] = new_w
            _rt_state["camera"]["height"] = new_h
            _save_runtime(_rt_state)
            current_rot = _rt_state["camera"].get("rotation_deg", 0)
            new_fw = _tracking_frame_width(new_w, new_h, current_rot)
            if tracking_svc is not None:
                tracking_svc.update_frame_width(new_fw)

    bus.subscribe("motion.enabled_changed",         _on_servo_changed)
    bus.subscribe("motion.limits_changed",          _on_limits_changed)
    bus.subscribe("tracking.face_tracking_changed", _on_face_tracking_changed)
    bus.subscribe("tracking.random_motion_changed", _on_random_motion_changed)
    bus.subscribe("camera.rotation_changed",        _on_camera_rotation_changed)
    bus.subscribe("camera2.rotation_changed",       _on_camera2_rotation_changed)
    bus.subscribe("camera.resolution_changed",      _on_camera_resolution_changed)

    tracking_svc: "TrackingService | None" = None  # forward-ref for rotation callback

    av = AVService(bus=bus)
    vis = VisionService(bus=bus, camera_config=_camera_cfg,
                        servo_min_deg=_soft_min_deg, servo_max_deg=_soft_max_deg)
    ipc = IPCBridge(
        bus=bus,
        upstream_endpoints=[_THERMAL_PUB],
    )
    ipc.register_rpc("tts_duration", lambda msg: {
        "ok": True,
        "duration_s": av.tts_duration_rpc(msg.get("text", "")),
    })

    motion_svc = MotionService(
        bus=bus, quiet_hours=_qh,
        servo_enabled=_servo_enabled,
        soft_min_deg=_soft_min_deg,
        soft_max_deg=_soft_max_deg,
        pulse_min_us=int(_servo_cfg.get("pulse_min_us", 500)),
        pulse_max_us=int(_servo_cfg.get("pulse_max_us", 2500)),
    )

    _music_cfg = _cfg.get("music", {})
    music_svc = MusicService(
        bus=bus,
        enabled=bool(_music_cfg.get("enabled", True)),
        announce_song_changes=bool(_music_cfg.get("announce_song_changes", False)),
    )

    # Second camera (optional — gracefully absent if not configured or detected)
    _cam2_cfg_raw = _cfg.get("camera2", {})
    cam2_svc = None
    if _cam2_cfg_raw.get("enabled", True):
        cam2_svc = RawCameraService(
            bus=bus,
            camera_config=RawCameraConfig(
                index=int(_cam2_cfg_raw.get("index", 1)),
                width=_cam_width,
                height=_cam_height,
                framerate=int(_cam2_cfg_raw.get("framerate", 15)),
                rotation_deg=_camera2_rotation_deg,  # from runtime state
                af_mode=str(_cam2_cfg_raw.get("af_mode", "continuous")),
                lens_position=float(_cam2_cfg_raw.get("lens_position", 0.0)),
            ),
        )

    obj_svc = ObjectService(bus=bus, vision_service=vis, config=_obj_cfg)
    perc_svc = PerceptionService(bus=bus, vision_service=vis, config=_perc_cfg)
    services = [
        motion_svc,
        vis,
        AudioCaptureService(bus=bus),
        av,
        perc_svc,
        obj_svc,
        TelemetryService(bus=bus),
        ClockService(bus=bus, enabled=_clock_enabled, quiet_hours=_qh),
        FaceService(
            bus=bus,
            greeting_cooldown_min=_greeting_cooldown_min,
            greeting_cooldown_jitter_pct=_greeting_jitter_pct,
            min_absence_s=_min_absence_s,
            confidence_threshold=_confidence_threshold,
            quiet_hours=_qh,
        ),
        music_svc,
    ]
    skills_svc = SkillsService(bus=bus, quiet_hours=_qh)
    services.append(skills_svc)
    if cam2_svc is not None:
        services.append(cam2_svc)
    tracking_svc = TrackingService(
        bus=bus, config=_tracker_cfg, enabled=_tracking_enabled,
        face_tracking_enabled=_face_tracking_enabled,
        random_motion_enabled=_random_motion_enabled,
        person_seek_enabled=_person_seek_enabled,
        speaking_motion_enabled=bool(_speaking_motion_cfg.get("enabled", True)),
        speaking_motion_amplitude_deg=float(_speaking_motion_cfg.get("amplitude_deg", 1.5)),
        speaking_motion_freq_hz=float(_speaking_motion_cfg.get("freq_hz", 2.5)),
    )
    services.append(tracking_svc)

    notif_svc = NotificationService(
        bus=bus,
        quiet_hours=_qh,
        thermal_alerts_enabled=bool(_notif_thermal_cfg.get("enabled", True)),
        warn_celsius=float(_notif_thermal_cfg.get("warn_celsius", 75.0)),
        critical_celsius=float(_notif_thermal_cfg.get("critical_celsius", 85.0)),
        thermal_rate_limit_min=float(_notif_thermal_cfg.get("min_interval_min", 10.0)),
        absence_alerts_enabled=bool(_notif_absence_cfg.get("enabled", True)),
        absence_min=float(_notif_absence_cfg.get("absence_min", 30.0)),
        absence_rate_limit_min=float(_notif_absence_cfg.get("min_interval_min", 60.0)),
    )
    services.append(notif_svc)
    services.append(ipc)
    ipc._all_services = services  # seed service registry at startup
    if _web_enabled:
        web_svc = WebService(bus=bus, host=_web_host, port=_web_port, vision_service=vis,
                             quiet_hours=_qh, motion_service=motion_svc,
                             tracking_service=tracking_svc, music_service=music_svc,
                             camera2_service=cam2_svc, object_service=obj_svc,
                             skills_service=skills_svc, perception_service=perc_svc)
        services.append(web_svc)
        web_svc._all_services = services  # seed service registry at startup

    return run_services(services=services, unit_name="core")


if __name__ == "__main__":
    sys.exit(main())
