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
from src.core.ipc_client import IPCClient
from src.core.media_client import MusicServiceProxy, PodcastServiceProxy
from src.core.integrations_client import IoTRegistryProxy, SkillsServiceProxy
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
from src.services.mono_depth_service import MonoDepthService
from src.services.telemetry_service import TelemetryService
from src.services.tracking_service import TrackingService
from src.services.vision_service import VisionService
from src.services.voice_command_service import VoiceCommandService, VoiceCommandConfig
from src.core.quiet_hours import QuietHours
from src.services.web_service import WebService
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
# bus events); IoT/skills need a REP endpoint core calls into (via
# IoTRegistryProxy/SkillsServiceProxy) since WebService held direct object
# references to them.
_INTEGRATIONS_PUB = "ipc:///tmp/desktop-assistant-integrations.pub"
_INTEGRATIONS_REP = "ipc:///tmp/desktop-assistant-integrations.rep"


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
    _person_seek_enabled = _cfg.get("head_tracking", {}).get("person_seek_enabled", True)
    _recognition_enabled = _cfg.get("face_recognition", {}).get("enabled", True)
    _fr_cfg = _cfg.get("face_recognition", {})
    _greeting_cooldown_min = float(_fr_cfg.get("greeting_cooldown_min", 30.0))
    _greeting_jitter_pct   = float(_fr_cfg.get("greeting_cooldown_jitter_pct", 25.0))
    _min_absence_s         = float(_fr_cfg.get("min_absence_s", 30.0))
    _confidence_threshold  = float(_fr_cfg.get("confidence_threshold", 0.5))
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
    _camera_cfg = _CameraConfig(
        width=_cam_width,
        height=_cam_height,
        framerate=int(_cam_cfg_raw.get("framerate", 30)),
        rotation_deg=_camera_rotation_deg,
        af_mode=str(_cam_cfg_raw.get("af_mode", "continuous")),
        lens_position=float(_cam_cfg_raw.get("lens_position", 0.0)),
        stream_width=int(_cam_cfg_raw.get("stream_width", 640)),
        stream_height=int(_cam_cfg_raw.get("stream_height", 360)),
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

    # Optional LED ring — only instantiated for respeaker_flex backend
    _led: "object | None" = None
    if _audio_backend == BACKEND_RESPEAKER_FLEX and _audio_backend_cfg.get("led_enabled", True):
        from src.audio.respeaker_flex import ReSpeakerFlexLED
        _led = ReSpeakerFlexLED(bus=bus, enabled=True)

    av = AVService(bus=bus, audio_output=_audio_out)

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
        upstream_endpoints=[_THERMAL_PUB, _MEDIA_PUB, _INTEGRATIONS_PUB],
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

    _media_client = IPCClient(_MEDIA_REP)
    music_proxy = MusicServiceProxy(_media_client)
    podcast_proxy = PodcastServiceProxy(_media_client)

    _integrations_client = IPCClient(_INTEGRATIONS_REP)
    iot_registry_proxy = IoTRegistryProxy(_integrations_client)
    skills_proxy = SkillsServiceProxy(_integrations_client)

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
    services = [
        motion_svc,
        vis,
        capture_svc,
        voice_svc,
        av,
        perc_svc,
        obj_svc,
        TelemetryService(bus=bus),
        RoomService(bus=bus, vision_service=vis, cfg=_room_cfg),
        FaceService(
            bus=bus,
            greeting_cooldown_min=_greeting_cooldown_min,
            greeting_cooldown_jitter_pct=_greeting_jitter_pct,
            min_absence_s=_min_absence_s,
            confidence_threshold=_confidence_threshold,
            quiet_hours=_qh,
            guest_intro_delay_min=0.0,  # gate is now in PerceptionService
        ),
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

    # IoTService and SkillsService moved to the "integrations" process
    # (Phase 2b of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md; see
    # src/assistant/integrations_main.py). WebService gets proxies instead
    # of direct object references.
    # NotificationService and TelegramService moved to the "integrations"
    # process (Phase 2a of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md;
    # see src/assistant/integrations_main.py). No proxy needed here since
    # WebService never held direct references to either.
    services.append(ipc)
    ipc._all_services = services  # seed service registry at startup
    if _web_enabled:
        room_svc = next((s for s in services if getattr(s, "name", "") == "room"), None)
        web_svc = WebService(bus=bus, host=_web_host, port=_web_port, vision_service=vis,
                             quiet_hours=_qh, motion_service=motion_svc,
                             tracking_service=tracking_svc, music_service=music_proxy,
                             podcast_service=podcast_proxy,
                             camera2_service=cam2_svc, object_service=obj_svc,
                             skills_service=skills_proxy, perception_service=perc_svc,
                             dense_stereo_service=dense_stereo_svc,
                             mono_depth_service=mono_depth_svc,
                             room_service=room_svc,
                             privacy_service=privacy_svc,
                             iot_registry=iot_registry_proxy)
        services.append(web_svc)
        web_svc._all_services = services  # seed service registry at startup

    return run_services(services=services, unit_name="core")


if __name__ == "__main__":
    sys.exit(main())
