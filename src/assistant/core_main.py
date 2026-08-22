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
from src.services.audio_capture_service import AudioCaptureService, AudioCaptureConfig
from src.services.av_service import AVService
from src.services.face_service import FaceService
from src.services.ipc_bridge import IPCBridge
from src.services.motion_service import MotionService
from src.services.object_service import ObjectService, ObjectConfig
from src.services.perception_service import PerceptionService, PerceptionConfig
from src.services.raw_camera_service import RawCameraService, RawCameraConfig
from src.services.stereo_service import StereoService, StereoConfig
from src.services.dense_stereo_service import DenseStereoService, DenseStereoConfig
from src.services.display_service import DisplayService, DisplayServiceConfig
from src.services.mono_depth_service import MonoDepthService
from src.services.telemetry_service import TelemetryService
from src.services.tracking_service import TrackingService
from src.services.vision_service import VisionService
from src.services.voice_command_service import VoiceCommandService, VoiceCommandConfig
from src.core.quiet_hours import QuietHours
from src.core.runtime_state import load as _load_runtime, save as _save_runtime
from src.services.room_service import RoomService
from src.services.privacy_service import PrivacyService, PrivacyConfig

# The thermal service runs in a separate process. Its IPCBridge PUBs on
# this endpoint; we SUBscribe to it from the core IPCBridge and re-emit
# events on our local bus so the CLI sees thermal.* topics in `status`.
_THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"

# The media service (music + podcasts) also runs in a separate process
# (Phase 1 of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md). Same
# upstream-forwarding pattern as thermal, plus a REP endpoint core calls
# into (via MusicServiceProxy/PodcastServiceProxy) for synchronous reads
# and actions that WebService needs a return value from.
_MEDIA_PUB = "ipc:///tmp/desktop-assistant-media.pub"
_MEDIA_REP = "ipc:///tmp/desktop-assistant-media.rep"

# Telegram/notification/clock/IoT/skills (the "integrations" group of
# docs/architecture/PROCESS_ISOLATION_PROPOSAL.md, Phases 2a+2b) also run
# in a separate process. Telegram/notification/clock need no proxy (pure
# bus events); IoT/skills need a REP endpoint the "web" process calls into
# (via IoTRegistryProxy/SkillsServiceProxy) since WebService holds direct
# object references to them.
_INTEGRATIONS_PUB = "ipc:///tmp/desktop-assistant-integrations.pub"
_INTEGRATIONS_REP = "ipc:///tmp/desktop-assistant-integrations.rep"

# WebService (the FastAPI dashboard) also runs in a separate process
# (Phase 3 of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md). Unlike the
# other splits, the direction is reversed here: `web` is the one calling
# *into* core (via the proxies in src/core/web_client.py) for the handful
# of services it still needs live reads/actions from (room, face, privacy,
# object detection, perception, motion, tracking, vision, camera2, depth
# enabled-flags) — core's own default IPCBridge REP endpoint
# (ipc:///tmp/desktop-assistant.rep, registered below) already serves this
# without a dedicated `_CORE_REP` constant. `_WEB_PUB` is added to core's
# upstream_endpoints so bus.publish() calls WebService makes (settings
# toggles, av.say, motion.pan_to, ...) still reach core's services.
_WEB_PUB = "ipc:///tmp/desktop-assistant-web.pub"


def main() -> int:
    import yaml
    from pathlib import Path
    _cfg_path = Path(__file__).parents[2] / "config" / "assistant.yaml"
    _cfg = yaml.safe_load(_cfg_path.read_text()) if _cfg_path.exists() else {}

    # Runtime state overlays the base config for toggle settings.
    _rt = _load_runtime()

    _tracking_enabled = _cfg.get("head_tracking", {}).get("enabled", True)
    _face_tracking_enabled = _rt.get("head_tracking", {}).get(
        "face_tracking_enabled",
        _cfg.get("head_tracking", {}).get("face_tracking_enabled", True),
    )
    _random_motion_enabled = _rt.get("head_tracking", {}).get(
        "random_motion_enabled",
        _cfg.get("head_tracking", {}).get("random_motion_enabled", True),
    )
    _person_seek_enabled = _rt.get("head_tracking", {}).get(
        "person_seek_enabled",
        _cfg.get("head_tracking", {}).get("person_seek_enabled", True),
    )
    _recognition_enabled = _cfg.get("face_recognition", {}).get("enabled", True)
    _fr_cfg = _cfg.get("face_recognition", {})
    _rt_fr = _rt.get("face_recognition", {})
    _greeting_cooldown_min = float(_rt_fr.get(
        "greeting_cooldown_min", _fr_cfg.get("greeting_cooldown_min", 30.0)
    ))
    _greeting_jitter_pct   = float(_rt_fr.get(
        "greeting_cooldown_jitter_pct", _fr_cfg.get("greeting_cooldown_jitter_pct", 25.0)
    ))
    _min_absence_s         = float(_rt_fr.get(
        "min_absence_s", _fr_cfg.get("min_absence_s", 30.0)
    ))
    _confidence_threshold  = float(_rt_fr.get(
        "confidence_threshold", _fr_cfg.get("confidence_threshold", 0.5)
    ))
    _guest_intro_delay_min = float(_fr_cfg.get("guest_intro_delay_min", 2.0))
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
    _cam_stream_width  = int(_rt.get("camera", {}).get(
        "stream_width",  _cam_cfg_raw.get("stream_width", 640)
    ))
    _cam_stream_height = int(_rt.get("camera", {}).get(
        "stream_height", _cam_cfg_raw.get("stream_height", 360)
    ))
    _camera_cfg = _CameraConfig(
        width=_cam_width,
        height=_cam_height,
        framerate=int(_cam_cfg_raw.get("framerate", 30)),
        rotation_deg=_camera_rotation_deg,
        af_mode=str(_cam_cfg_raw.get("af_mode", "continuous")),
        lens_position=float(_cam_cfg_raw.get("lens_position", 0.0)),
        stream_width=_cam_stream_width,
        stream_height=_cam_stream_height,
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
        dead_zone_frac=float(_ht_cfg_raw.get("dead_zone_frac", 0.06)),
        tracking_gain=float(_ht_cfg_raw.get("tracking_gain", 0.92)),
        max_speed_deg_s=float(_ht_cfg_raw.get("max_speed_deg_s", 250.0)),
        invert_pan=bool(_ht_cfg_raw.get("invert_pan", False)),
        kalman_r=float(_ht_cfg_raw.get("kalman_r", 400.0)),
        kalman_q_pos=float(_ht_cfg_raw.get("kalman_q_pos", 1.0)),
        kalman_q_vel=float(_ht_cfg_raw.get("kalman_q_vel", 50.0)),
        lookahead_s=float(_ht_cfg_raw.get("lookahead_s", 0.05)),
        replan_threshold_deg=float(_ht_cfg_raw.get("replan_threshold_deg", 2.0)),
        move_base_s=float(_ht_cfg_raw.get("move_base_s", 0.15)),
        move_scale_s_per_deg=float(_ht_cfg_raw.get("move_scale_s_per_deg", 0.005)),
        move_max_s=float(_ht_cfg_raw.get("move_max_s", 0.55)),
    )
    _depth_cfg_raw = _cfg.get("depth", {})
    _perc_cfg = PerceptionConfig(
        recognition_enabled=_recognition_enabled,
        max_fps=float(_fr_cfg.get("max_fps", 5.0)),
        match_threshold=float(_fr_cfg.get("match_threshold", 0.50)),
        min_face_px=int(_fr_cfg.get("min_face_px", 80)),
        fov_degrees=float(_ht_cfg_raw.get("fov_degrees", 100.0)),
        frame_width=_tracking_frame_width(_cam_width, _cam_height, _camera_rotation_deg),
        known_face_width_m=float(_depth_cfg_raw.get("known_face_width_m", 0.145)),
        min_depth_m=float(_depth_cfg_raw.get("min_depth_m", 0.25)),
        max_depth_m=float(_depth_cfg_raw.get("max_depth_m", 6.0)),
        guest_intro_delay_s=_guest_intro_delay_min * 60.0,
    )

    _obj_cfg_raw = _cfg.get("object_detection", {})
    _obj_cfg = ObjectConfig(
        enabled=bool(_obj_cfg_raw.get("enabled", True)),
        max_fps=float(_obj_cfg_raw.get("max_fps", 2.0)),
        conf_threshold=float(_obj_cfg_raw.get("conf_threshold", 0.40)),
        max_objects=int(_obj_cfg_raw.get("max_objects", 8)),
    )

    _room_cfg = _cfg.get("room_detection", {})

    _anthropic_cfg = _cfg.get("anthropic_api", {})
    _anthropic_enabled = bool(_anthropic_cfg.get("enabled", True))

    # web_dashboard config (host/port/enabled) is now read by
    # src/assistant/web_main.py, which runs WebService in its own process
    # (Phase 3 of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md).

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
            "person_seek_enabled": _person_seek_enabled,
        },
        "camera": {
            "rotation_deg": _camera_rotation_deg,
            "width": _cam_width,
            "height": _cam_height,
            "stream_width": _cam_stream_width,
            "stream_height": _cam_stream_height,
        },
        "camera2": {
            "rotation_deg": _camera2_rotation_deg,
        },
        "face_recognition": {
            "greeting_cooldown_min": _greeting_cooldown_min,
            "greeting_cooldown_jitter_pct": _greeting_jitter_pct,
            "min_absence_s": _min_absence_s,
            "confidence_threshold": _confidence_threshold,
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

    def _on_person_seek_changed(_t, payload):
        if isinstance(payload, dict) and "enabled" in payload:
            _rt_state["head_tracking"]["person_seek_enabled"] = bool(payload["enabled"])
            _save_runtime(_rt_state)

    def _on_greeting_cooldown_changed(_t, payload):
        if not isinstance(payload, dict):
            return
        fr = _rt_state["face_recognition"]
        if "cooldown_min" in payload:
            fr["greeting_cooldown_min"] = float(payload["cooldown_min"])
        if "jitter_pct" in payload:
            fr["greeting_cooldown_jitter_pct"] = float(payload["jitter_pct"])
        if "min_absence_s" in payload:
            fr["min_absence_s"] = float(payload["min_absence_s"])
        if "confidence_threshold" in payload:
            fr["confidence_threshold"] = float(payload["confidence_threshold"])
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

    def _on_camera_stream_resolution_changed(_t, payload):
        if isinstance(payload, dict) and "width" in payload and "height" in payload:
            _rt_state["camera"]["stream_width"] = int(payload["width"])
            _rt_state["camera"]["stream_height"] = int(payload["height"])
            _save_runtime(_rt_state)

    bus.subscribe("motion.enabled_changed",         _on_servo_changed)
    bus.subscribe("motion.limits_changed",          _on_limits_changed)
    bus.subscribe("tracking.face_tracking_changed", _on_face_tracking_changed)
    bus.subscribe("tracking.random_motion_changed", _on_random_motion_changed)
    bus.subscribe("tracking.person_seek_changed",   _on_person_seek_changed)
    bus.subscribe("tracking.greeting_cooldown_changed", _on_greeting_cooldown_changed)
    bus.subscribe("camera.rotation_changed",        _on_camera_rotation_changed)
    bus.subscribe("camera2.rotation_changed",       _on_camera2_rotation_changed)
    bus.subscribe("camera.resolution_changed",      _on_camera_resolution_changed)
    bus.subscribe("camera.stream_resolution_changed", _on_camera_stream_resolution_changed)

    tracking_svc: "TrackingService | None" = None  # forward-ref for rotation callback

    # ── Audio backend selection ───────────────────────────────────────────────
    from src.audio.factory import (
        create_audio_input, create_audio_output,
        BACKEND_DEFAULT, BACKEND_RESPEAKER_FLEX,
    )
    _audio_cfg = _cfg.get("audio", {})
    _audio_backend = str(_audio_cfg.get("backend", BACKEND_DEFAULT))
    _audio_backend_cfg = _audio_cfg.get(_audio_backend, {})
    _audio_out = create_audio_output(_audio_backend, _audio_backend_cfg)
    _audio_in  = create_audio_input(_audio_backend, _audio_backend_cfg)
    _audio_capture_cfg = _cfg.get("audio_capture", {})
    _voice_cfg_raw = _cfg.get("voice_commands", {})
    _display_cfg_raw = _cfg.get("display", {})

    # Optional LED ring — only instantiated for respeaker_flex backend
    _led: "object | None" = None
    if _audio_backend == BACKEND_RESPEAKER_FLEX and _audio_backend_cfg.get("led_enabled", True):
        from src.audio.respeaker_flex import ReSpeakerFlexLED
        _led = ReSpeakerFlexLED(bus=bus, enabled=True)

    av = AVService(bus=bus, audio_output=_audio_out)
    display_svc = DisplayService(
        bus=bus,
        config=DisplayServiceConfig(
            enabled=bool(_display_cfg_raw.get("enabled", True)),
            ble_enabled=bool(_display_cfg_raw.get("ble_enabled", False)),
            ble_address=str(_display_cfg_raw.get("ble_address", "")),
            ble_characteristic_uuid=str(_display_cfg_raw.get("ble_characteristic_uuid", "")),
            connect_timeout_s=float(_display_cfg_raw.get("connect_timeout_s", 4.0)),
            max_message_chars=int(_display_cfg_raw.get("max_message_chars", 96)),
            expected_services=list(_display_cfg_raw.get("expected_services", [])),
        ),
    )

    # Wire LED ring to speech activity if respeaker_flex backend is active
    if _led is not None:
        from src.audio.respeaker_flex import (
            LED_STATE_IDLE, LED_STATE_SPEAKING, ReSpeakerFlexLED,
        )
        def _on_av_say(_topic, _payload, _led=_led):
            _led.set_state(LED_STATE_SPEAKING)
        def _on_av_spoke(_topic, _payload, _led=_led):
            _led.set_state(LED_STATE_IDLE)
        bus.subscribe("av.say",   _on_av_say)
        bus.subscribe("av.spoke", _on_av_spoke)
    vis = VisionService(bus=bus, camera_config=_camera_cfg,
                        servo_min_deg=_soft_min_deg, servo_max_deg=_soft_max_deg)
    ipc = IPCBridge(
        bus=bus,
        upstream_endpoints=[_THERMAL_PUB, _MEDIA_PUB, _INTEGRATIONS_PUB, _WEB_PUB],
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
                stream_width=int(_cam_cfg_raw.get("stream_width", 640)),
                stream_height=int(_cam_cfg_raw.get("stream_height", 360)),
            ),
        )

    obj_svc = ObjectService(bus=bus, vision_service=vis, config=_obj_cfg)
    perc_svc = PerceptionService(bus=bus, vision_service=vis, config=_perc_cfg)
    capture_svc = AudioCaptureService(
        bus=bus,
        mic=_audio_in,
        config=AudioCaptureConfig(
            chunk_seconds=float(_audio_capture_cfg.get("chunk_seconds", 0.25)),
            spectrum_bins=int(_audio_capture_cfg.get("spectrum_bins", 48)),
            emit_spectrum=bool(_audio_capture_cfg.get("emit_spectrum", True)),
            vad_threshold_dbfs=float(_audio_capture_cfg.get("vad_threshold_dbfs", -42.0)),
            vad_hang_s=float(_audio_capture_cfg.get("vad_hang_s", 0.8)),
        ),
    )
    voice_svc = VoiceCommandService(
        bus=bus,
        capture_service=capture_svc,
        led=_led,
        config=VoiceCommandConfig(
            enabled=bool(_voice_cfg_raw.get("enabled", False)),
            poll_seconds=float(_voice_cfg_raw.get("poll_seconds", 0.08)),
            sample_rate=int(_voice_cfg_raw.get("sample_rate", 16000)),
            wake_cooldown_s=float(_voice_cfg_raw.get("wake_cooldown_s", 1.5)),
            wake_threshold_dbfs=float(_voice_cfg_raw.get("wake_threshold_dbfs", -38.0)),
            wake_consecutive_frames=int(_voice_cfg_raw.get("wake_consecutive_frames", 2)),
            wake_backend=str(_voice_cfg_raw.get("wake_backend", "energy")),
            oww_model=str(_voice_cfg_raw.get("oww_model", "hey_jarvis_v0.1")),
            oww_threshold=float(_voice_cfg_raw.get("oww_threshold", 0.5)),
            oww_refractory_s=float(_voice_cfg_raw.get("oww_refractory_s", 2.0)),
            command_min_s=float(_voice_cfg_raw.get("command_min_s", 0.35)),
            command_max_s=float(_voice_cfg_raw.get("command_max_s", 6.0)),
            silence_end_s=float(_voice_cfg_raw.get("silence_end_s", 0.8)),
            stt_backend=str(_voice_cfg_raw.get("stt_backend", "faster_whisper")),
            stt_command=str(_voice_cfg_raw.get("stt_command", "")),
            stt_language=str(_voice_cfg_raw.get("stt_language", "en")),
            stt_timeout_s=float(_voice_cfg_raw.get("stt_timeout_s", 20.0)),
            stt_model=str(_voice_cfg_raw.get("stt_model", "base.en")),
            stt_device=str(_voice_cfg_raw.get("stt_device", "cpu")),
            stt_compute_type=str(_voice_cfg_raw.get("stt_compute_type", "int8")),
            stt_cpu_threads=int(_voice_cfg_raw.get("stt_cpu_threads", 2)),
            dialog_timeout_s=float(_voice_cfg_raw.get("dialog_timeout_s", 20.0)),
        ),
    )
    # Let AVService record clips from the already-running capture stream
    # instead of opening a second (conflicting) input device.
    av.set_capture_service(capture_svc)
    room_svc = RoomService(bus=bus, vision_service=vis, cfg=_room_cfg, anthropic_enabled=_anthropic_enabled)
    face_svc = FaceService(
        bus=bus,
        greeting_cooldown_min=_greeting_cooldown_min,
        greeting_cooldown_jitter_pct=_greeting_jitter_pct,
        min_absence_s=_min_absence_s,
        confidence_threshold=_confidence_threshold,
        quiet_hours=_qh,
        guest_intro_delay_min=0.0,  # gate is now in PerceptionService
        anthropic_enabled=_anthropic_enabled,
    )
    services = [
        display_svc,
        motion_svc,
        vis,
        capture_svc,
        voice_svc,
        av,
        perc_svc,
        obj_svc,
        TelemetryService(bus=bus),
        room_svc,
        face_svc,
    ]
    if cam2_svc is not None:
        services.append(cam2_svc)
    # Stereo depth service — runs only when cam2 is present and depth is enabled
    if cam2_svc is not None and _depth_cfg_raw.get("enabled", True):
        stereo_svc = StereoService(
            bus=bus,
            vision_service=vis,
            cam2_service=cam2_svc,
            config=StereoConfig(
                baseline_mm=float(_depth_cfg_raw.get("baseline_mm", 56.0)),
                known_face_width_m=float(_depth_cfg_raw.get("known_face_width_m", 0.145)),
                fov_degrees=float(_ht_cfg_raw.get("fov_degrees", 100.0)),
                frame_width=_tracking_frame_width(_cam_width, _cam_height, _camera_rotation_deg),
                frame_height=_cam_height,
                min_depth_m=float(_depth_cfg_raw.get("min_depth_m", 0.25)),
                max_depth_m=float(_depth_cfg_raw.get("max_depth_m", 6.0)),
            ),
        )
        services.append(stereo_svc)
    # Dense stereo depth service — StereoSGBM per-pixel depth map
    # Always started when cam2 is available; enabled/disabled at runtime via bus.
    if cam2_svc is not None:
        dense_stereo_svc = DenseStereoService(
            bus=bus,
            vision_service=vis,
            cam2_service=cam2_svc,
            config=DenseStereoConfig(
                rate_hz=float(_depth_cfg_raw.get("dense_rate_hz", 3.0)),
                proc_width=int(_depth_cfg_raw.get("dense_width", 640)),
                proc_height=int(_depth_cfg_raw.get("dense_height", 480)),
                num_disparities=int(_depth_cfg_raw.get("num_disparities", 128)),
                block_size=int(_depth_cfg_raw.get("block_size", 5)),
                min_depth_m=float(_depth_cfg_raw.get("min_depth_m", 0.25)),
                max_depth_m=float(_depth_cfg_raw.get("max_depth_m", 6.0)),
                baseline_mm=float(_depth_cfg_raw.get("baseline_mm", 56.0)),
                fov_degrees=float(_ht_cfg_raw.get("fov_degrees", 100.0)),
                enabled=bool(_depth_cfg_raw.get("dense_enabled", False)),
            ),
        )
        services.append(dense_stereo_svc)
    else:
        dense_stereo_svc = None
    # Monocular depth service — Hailo-8 scdepthv3 single-camera depth.
    # Always started; enabled/disabled at runtime via depth.set_mono_enabled bus topic.
    mono_depth_svc = MonoDepthService(
        bus=bus,
        vision_service=vis,
        config={"depth": _depth_cfg_raw},
    )
    services.append(mono_depth_svc)
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

    _privacy_cfg_raw = _cfg.get("privacy", {})
    privacy_svc = PrivacyService(
        bus=bus,
        vision_service=vis,
        config=PrivacyConfig(
            enabled=bool(_privacy_cfg_raw.get("enabled", True)),
            rate_hz=float(_privacy_cfg_raw.get("rate_hz", 1.0)),
            idle_rate_hz=float(_privacy_cfg_raw.get("idle_rate_hz", 0.25)),
            threshold=float(_privacy_cfg_raw.get("threshold", 0.6)),
            look_away_angle_deg=float(_privacy_cfg_raw.get("look_away_angle_deg", 45.0)),
            cooldown_s=float(_privacy_cfg_raw.get("cooldown_s", 10.0)),
            clear_frames=int(_privacy_cfg_raw.get("clear_frames", 3)),
            require_person=bool(_privacy_cfg_raw.get("require_person", True)),
            person_hold_s=float(_privacy_cfg_raw.get("person_hold_s", 8.0)),
            announce=bool(_privacy_cfg_raw.get("announce", True)),
            announce_text=str(_privacy_cfg_raw.get("announce_text", "I'll give you some privacy.")),
            resume_text=str(_privacy_cfg_raw.get("resume_text", "")),
        ),
    )
    services.append(privacy_svc)

    if not _display_cfg_raw.get("expected_services"):
        display_svc.set_expected_services([s.name for s in services if s is not display_svc])

    # IoTService and SkillsService moved to the "integrations" process
    # (Phase 2b of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md; see
    # src/assistant/integrations_main.py).
    # NotificationService and TelegramService moved to the "integrations"
    # process (Phase 2a of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md;
    # see src/assistant/integrations_main.py).
    services.append(ipc)
    ipc._all_services = services  # seed service registry at startup

    # ── WebService RPCs (Phase 3) ─────────────────────────────────────
    # WebService itself now runs in the "web" process (src/assistant/
    # web_main.py) and reaches these still-in-core services through the
    # proxies in src/core/web_client.py. See that module's docstring for
    # the full rationale (most of these are read-mostly; the paired
    # PUT/action routes already went through self.bus.publish() and cross
    # the process boundary for free via IPCBridge).

    def _rpc_room_get_status(_msg):
        return {"ok": True, "status": room_svc.get_status_dict()}

    def _rpc_face_get_anthropic_enabled(_msg):
        return {"ok": True, "enabled": bool(face_svc._anthropic_enabled)}

    def _rpc_face_get_greeting_settings(_msg):
        return {
            "ok": True,
            "cooldown_min":         getattr(face_svc, "_cooldown_min", 30.0),
            "jitter_pct":           getattr(face_svc, "_jitter_pct", 25.0),
            "min_absence_s":        getattr(face_svc, "_min_absence_s", 30.0),
            "confidence_threshold": getattr(face_svc, "_confidence_threshold", 0.5),
        }

    def _rpc_privacy_get_status(_msg):
        svc = privacy_svc
        return {
            "ok": True,
            "status": {
                "enabled":             getattr(svc, "_enabled", True),
                "hardware_ready":      getattr(svc, "hardware_ready", False),
                "rate_hz":             getattr(getattr(svc, "_cfg", None), "rate_hz", 1.0),
                "idle_rate_hz":        getattr(getattr(svc, "_cfg", None), "idle_rate_hz", 0.25),
                "threshold":           getattr(getattr(svc, "_cfg", None), "threshold", 0.6),
                "look_away_angle_deg": getattr(getattr(svc, "_cfg", None), "look_away_angle_deg", 45.0),
                "cooldown_s":          getattr(getattr(svc, "_cfg", None), "cooldown_s", 10.0),
                "clear_frames":        getattr(getattr(svc, "_cfg", None), "clear_frames", 3),
                "require_person":      getattr(getattr(svc, "_cfg", None), "require_person", True),
                "person_hold_s":       getattr(getattr(svc, "_cfg", None), "person_hold_s", 8.0),
                "announce":            getattr(getattr(svc, "_cfg", None), "announce", True),
                "announce_text":       getattr(getattr(svc, "_cfg", None), "announce_text",
                                                "I'll give you some privacy."),
                "resume_text":         getattr(getattr(svc, "_cfg", None), "resume_text", ""),
            },
        }

    def _rpc_object_get_enabled(_msg):
        return {"ok": True, "enabled": bool(obj_svc.detection_enabled)}

    def _rpc_perception_capture_training_image(msg):
        face_id = msg.get("face_id", "")
        result = perc_svc.capture_training_image(face_id)
        return {"ok": True, "result": result}

    def _rpc_motion_get_status(_msg):
        return {
            "ok": True,
            "status": {
                "servo_enabled": motion_svc.servo_enabled,
                "soft_min_deg":  motion_svc.soft_min_deg,
                "soft_max_deg":  motion_svc.soft_max_deg,
            },
        }

    def _rpc_tracking_get_status(_msg):
        return {
            "ok": True,
            "status": {
                "face_tracking_enabled": tracking_svc.face_tracking_enabled,
                "random_motion_enabled": tracking_svc.random_motion_enabled,
                "person_seek_enabled":   tracking_svc.person_seek_enabled,
            },
        }

    def _rpc_tracking_get_tunable_params(_msg):
        return {"ok": True, "data": tracking_svc.get_tunable_params()}

    def _rpc_tracking_set_tunable_param(msg):
        applied = bool(tracking_svc.set_tunable_param(msg.get("name"), msg.get("value")))
        return {"ok": True, "applied": applied}

    def _rpc_vision_get_status(_msg):
        w, h = vis.resolution
        sw, sh = vis.stream_resolution
        return {
            "ok": True,
            "status": {
                "rotation_deg": vis.rotation_deg,
                "resolution": [w, h],
                "stream_resolution": [sw, sh],
            },
        }

    def _rpc_vision_latest_jpeg(_msg):
        import base64
        jpeg = vis.latest_jpeg()
        return {"ok": True, "jpeg_b64": base64.b64encode(jpeg).decode("ascii") if jpeg else None}

    def _rpc_vision_snapshot_jpeg(msg):
        import base64
        import cv2
        frame = vis.latest_frame()
        if frame is None:
            return {"ok": False, "error": "no frame available"}
        quality = int(msg.get("quality", 95))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return {"ok": False, "error": "JPEG encode failed"}
        return {"ok": True, "jpeg_b64": base64.b64encode(bytes(buf)).decode("ascii")}

    def _rpc_camera2_get_status(_msg):
        if cam2_svc is None:
            return {"ok": True, "configured": False, "status": {}}
        w, h = cam2_svc.resolution
        sw, sh = cam2_svc.stream_resolution
        return {
            "ok": True,
            "configured": True,
            "status": {
                "rotation_deg": cam2_svc.rotation_deg,
                "resolution": [w, h],
                "stream_resolution": [sw, sh],
            },
        }

    def _rpc_camera2_latest_jpeg(_msg):
        import base64
        if cam2_svc is None:
            return {"ok": False, "error": "camera 2 not enabled"}
        jpeg = cam2_svc.latest_jpeg()
        return {"ok": True, "jpeg_b64": base64.b64encode(jpeg).decode("ascii") if jpeg else None}

    def _rpc_camera2_snapshot_jpeg(msg):
        import base64
        import cv2
        if cam2_svc is None:
            return {"ok": False, "error": "camera 2 not enabled"}
        frame = cam2_svc.latest_frame()
        if frame is None:
            return {"ok": False, "error": "no frame available"}
        quality = int(msg.get("quality", 95))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if not ok:
            return {"ok": False, "error": "JPEG encode failed"}
        return {"ok": True, "jpeg_b64": base64.b64encode(bytes(buf)).decode("ascii")}

    def _rpc_depth_get_enabled_flags(_msg):
        return {
            "ok": True,
            "flags": {
                "dense_enabled": getattr(dense_stereo_svc, "_enabled", False),
                "mono_enabled":  getattr(mono_depth_svc, "_enabled", False),
            },
        }

    ipc.register_rpc("room.get_status", _rpc_room_get_status)
    ipc.register_rpc("face.get_anthropic_enabled", _rpc_face_get_anthropic_enabled)
    ipc.register_rpc("face.get_greeting_settings", _rpc_face_get_greeting_settings)
    ipc.register_rpc("privacy.get_status", _rpc_privacy_get_status)
    ipc.register_rpc("object.get_enabled", _rpc_object_get_enabled)
    ipc.register_rpc("perception.capture_training_image", _rpc_perception_capture_training_image)
    ipc.register_rpc("motion.get_status", _rpc_motion_get_status)
    ipc.register_rpc("tracking.get_status", _rpc_tracking_get_status)
    ipc.register_rpc("tracking.get_tunable_params", _rpc_tracking_get_tunable_params)
    ipc.register_rpc("tracking.set_tunable_param", _rpc_tracking_set_tunable_param)
    ipc.register_rpc("vision.get_status", _rpc_vision_get_status)
    ipc.register_rpc("vision.latest_jpeg", _rpc_vision_latest_jpeg)
    ipc.register_rpc("vision.snapshot_jpeg", _rpc_vision_snapshot_jpeg)
    ipc.register_rpc("camera2.get_status", _rpc_camera2_get_status)
    ipc.register_rpc("camera2.latest_jpeg", _rpc_camera2_latest_jpeg)
    ipc.register_rpc("camera2.snapshot_jpeg", _rpc_camera2_snapshot_jpeg)
    ipc.register_rpc("depth.get_enabled_flags", _rpc_depth_get_enabled_flags)

    return run_services(services=services, unit_name="core")


if __name__ == "__main__":
    sys.exit(main())
