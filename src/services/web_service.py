"""
Web dashboard service — FastAPI app running inside the assistant process.

Serves a live dark-themed single-page dashboard on ``http://<pi>:8080``.

Endpoints
---------
GET  /                    Main dashboard HTML
GET  /stream              MJPEG camera stream (from bus vision.jpeg_ready frames)
WS   /ws                  Live JSON status + event tail (pushes every ~1 s)
GET  /health                 Liveness check {"ok":true}
GET  /api/status          One-shot status snapshot
GET  /api/faces           List all known faces
GET  /api/faces/{id}/thumb  Face thumbnail JPEG (64×64)
GET  /api/faces/{id}/photo  Full-size face photo JPEG (falls back to thumb)
PUT  /api/faces/{id}      Rename a face  body: {"name": "Alice"}
DEL  /api/faces           Delete ALL faces
DEL  /api/faces/guests    Delete only Guest-named faces
POST /api/faces/merge     Merge two faces  body: {"keep_id": "...", "absorb_id": "..."}
POST /api/faces/refresh   Reload embedding cache + reset tracking state (re-identify all faces)
DEL  /api/faces/{id}      Delete a face and all its embeddings
POST /api/faces/{id}/train  Capture current frame and add embedding/thumbnail to face
POST /api/say             Speak text   body: {"text": "hello"}
POST /api/audio/record    Record microphone to WAV  body: {"seconds": float, "path": str?}
POST /api/audio/playback  Play latest/specified WAV body: {"path": str?}
POST /api/pan             Pan servo    body: {"angle": 180.0}
GET  /api/snapshot            Full-resolution JPEG snapshot from camera 1
GET  /api/snapshot2           Full-resolution JPEG snapshot from camera 2
POST /api/version             Speak version number
POST /api/joke               Speak a random dad joke
POST /api/time               Announce the current time
GET  /api/settings/servo  Get servo enabled state
PUT  /api/settings/servo  Set servo enabled state  body: {"enabled": bool}
GET  /api/settings/servo/limits  Get servo travel limits
PUT  /api/settings/servo/limits  Set servo travel limits  body: {"min_deg": float, "max_deg": float}
GET  /api/settings/face-tracking  Get face tracking enabled state
PUT  /api/settings/face-tracking  Set face tracking  body: {"enabled": bool}
GET  /api/settings/random-motion  Get random motion enabled state
PUT  /api/settings/random-motion  Set random motion  body: {"enabled": bool}
GET  /api/settings/object-detection  Get object detection enabled state
PUT  /api/settings/object-detection  Set object detection  body: {"enabled": bool}
GET  /api/settings/fan/control-points  Get fan control points
PUT  /api/settings/fan/control-points  Set fan control points body: {"points": [{"temp_c": float, "duty": float}]}
GET  /api/settings/fan/temp-blend      Get temp blend weights
PUT  /api/settings/fan/temp-blend      Set temp blend weights  body: {"case_weight": float, "cpu_weight": float}
GET  /api/settings/greeting  Get greeting config
PUT  /api/settings/greeting  Update greeting cooldown  body: {"cooldown_min": float}
POST /api/vision/describe    Speak natural-language description of current scene
GET  /api/settings/camera/rotation   Get camera 1 rotation angle
PUT  /api/settings/camera/rotation   Set camera 1 rotation  body: {"rotation_deg": int 0-359}
GET  /api/settings/camera2/rotation  Get camera 2 rotation angle
PUT  /api/settings/camera2/rotation  Set camera 2 rotation  body: {"rotation_deg": int 0-359}
GET  /api/settings/camera/resolution   Get camera 1 capture resolution
PUT  /api/settings/camera/resolution   Set camera 1 capture resolution  body: {"width": int, "height": int}
GET  /api/settings/camera/stream_resolution   Get camera 1 MJPEG stream resolution
PUT  /api/settings/camera/stream_resolution   Set camera 1 MJPEG stream resolution  body: {"width": int, "height": int}
GET  /api/settings/camera2/resolution  Get camera 2 capture resolution
PUT  /api/settings/camera2/resolution  Set camera 2 capture resolution  body: {"width": int, "height": int}
GET  /api/settings/camera2/stream_resolution  Get camera 2 MJPEG stream resolution
PUT  /api/settings/camera2/stream_resolution  Set camera 2 MJPEG stream resolution  body: {"width": int, "height": int}
GET  /api/settings/depth     Get depth estimation settings (dense_enabled, mono_enabled, calibrated)
PUT  /api/settings/depth     Toggle depth at runtime  body: {"dense_enabled": bool, "mono_enabled": bool}
GET  /api/depth/map          Colorized depth map JPEG (TURBO colormap) — requires dense_enabled
GET  /api/depth/mono         Colorized mono depth map JPEG (TURBO colormap) — requires mono_enabled
GET  /api/depth/query        Depth statistics: nearest/farthest/mean + per-face depths
GET  /api/music/eq/custom  Get current custom EQ bands
PUT  /api/music/eq/custom  Set custom EQ bands  body: {"bands": [...]}
GET  /api/settings/audio   Get audio backend + all per-backend settings + available devices
PUT  /api/settings/audio   Set audio backend and/or per-backend settings  body: {"backend": str, "default"?: {...}, "respeaker_flex"?: {...}}
"""

import asyncio
import collections
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

import psutil
import zmq

from src.core.quiet_hours import QuietHours
from src.services.object_service import _build_scene_description

from pydantic import BaseModel

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "web" / "static"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_THERMAL_CONFIG_PATH = _PROJECT_ROOT / "config" / "thermal.yaml"
_ASSISTANT_CONFIG_PATH = _PROJECT_ROOT / "config" / "assistant.yaml"
_THERMAL_REP_ENDPOINT = "ipc:///tmp/desktop-assistant-thermal.rep"


class _RenameBody(BaseModel):
    name: str


class _RoomBody(BaseModel):
    name: str


class _QuietHoursBody(BaseModel):
    enabled: bool
    start: str
    end: str


class _SayBody(BaseModel):
    text: str


class _PanBody(BaseModel):
    angle: float


class _RecordBody(BaseModel):
    seconds: float = 5.0
    path: Optional[str] = None


class _PlaybackBody(BaseModel):
    path: Optional[str] = None


class _MergeFacesBody(BaseModel):
    keep_id: str
    absorb_id: str


class _ServoBody(BaseModel):
    enabled: bool


class _ServoLimitsBody(BaseModel):
    min_deg: float
    max_deg: float


class _GreetingBody(BaseModel):
    cooldown_min: float
    jitter_pct: Optional[float] = None
    min_absence_s: Optional[float] = None
    confidence_threshold: Optional[float] = None


class _CameraRotationBody(BaseModel):
    rotation_deg: int


class _CameraResolutionBody(BaseModel):
    width: int
    height: int


class _MusicVolumeBody(BaseModel):
    level: int


class _MusicEqBody(BaseModel):
    preset: str


class _CustomEqBand(BaseModel):
    hz: float
    gain_db: float
    q: float = 1.0


class _CustomEqBody(BaseModel):
    bands: list[_CustomEqBand]


class _SkillEnabledBody(BaseModel):
    enabled: bool


class _SkillConfigBody(BaseModel):
    key: str
    value: object


class _TrackingParamBody(BaseModel):
    name: str
    value: float


class _TrackingPresetBody(BaseModel):
    name: str


class _FanControlPoint(BaseModel):
    temp_c: float
    duty: float


class _FanControlPointsBody(BaseModel):
    points: list[_FanControlPoint]


class _TempBlendBody(BaseModel):
    case_weight: float
    cpu_weight: float


# ── Audio backend settings body ───────────────────────────────────────────────

class _AudioDefaultSettings(BaseModel):
    input_device_name: str = ""
    input_sample_rate: int = 44100
    output_alsa_device: str = "pulse"
    output_sample_rate: int = 44100
    loudness_boost: float = 2.0
    eq_preset: str = "flat"


class _AudioReSpeakerSettings(BaseModel):
    input_device_name: str = "ReSpeaker"
    input_sample_rate: int = 16000
    input_raw_channels: int = 6
    input_processed_channel: int = 0
    output_alsa_device: str = "pulse"
    output_sample_rate: int = 44100
    loudness_boost: float = 2.0
    eq_preset: str = "flat"
    led_enabled: bool = True


class _AudioSettingsBody(BaseModel):
    backend: str = "default"
    default: Optional[_AudioDefaultSettings] = None
    respeaker_flex: Optional[_AudioReSpeakerSettings] = None


def _normalise_fan_control_points(points: list[dict]) -> list[dict[str, float]]:
    if len(points) < 2:
        raise ValueError("At least two control points are required")
    cleaned: list[tuple[float, float]] = []
    for point in points:
        temp_c = float(point["temp_c"])
        duty = max(0.0, min(100.0, float(point["duty"])))
        cleaned.append((temp_c, duty))
    cleaned.sort(key=lambda p: p[0])
    dedup: dict[float, float] = {}
    for temp_c, duty in cleaned:
        dedup[temp_c] = duty
    output = [{"temp_c": float(t), "duty": float(d)} for t, d in sorted(dedup.items(), key=lambda p: p[0])]
    if len(output) < 2:
        raise ValueError("At least two unique temperature points are required")
    return output


def _thermal_request(req: dict, timeout_ms: int = 1500) -> dict:
    ctx = zmq.Context.instance()
    sock = ctx.socket(zmq.REQ)
    sock.setsockopt(zmq.LINGER, 0)
    sock.setsockopt(zmq.RCVTIMEO, timeout_ms)
    sock.setsockopt(zmq.SNDTIMEO, timeout_ms)
    try:
        sock.connect(_THERMAL_REP_ENDPOINT)
        sock.send_string(json.dumps(req))
        return json.loads(sock.recv_string())
    except zmq.error.Again:
        return {"ok": False, "error": "timeout — thermal service unavailable"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    finally:
        sock.close(linger=0)


def _read_fan_control_points_from_config() -> list[dict[str, float]]:
    import yaml

    with open(_THERMAL_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    thresholds = cfg.get("thresholds", {})
    raw = thresholds.get("control_points")
    if isinstance(raw, list):
        parsed = []
        for item in raw:
            if isinstance(item, dict) and "temp_c" in item and "duty" in item:
                parsed.append({"temp_c": item["temp_c"], "duty": item["duty"]})
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                parsed.append({"temp_c": item[0], "duty": item[1]})
        if len(parsed) >= 2:
            return _normalise_fan_control_points(parsed)

    safe_max_c = float(thresholds.get("safe_max_c", 50.0))
    warn_max_c = float(thresholds.get("warn_max_c", 65.0))
    critical_c = float(thresholds.get("critical_c", 75.0))
    fan_min_duty = float(thresholds.get("fan_min_duty", 30.0))
    fan_max_duty = float(thresholds.get("fan_max_duty", 100.0))
    if critical_c <= safe_max_c:
        return _normalise_fan_control_points([
            {"temp_c": safe_max_c, "duty": fan_min_duty},
            {"temp_c": safe_max_c + 1.0, "duty": fan_max_duty},
        ])
    warn_ratio = max(0.0, min(1.0, (warn_max_c - safe_max_c) / (critical_c - safe_max_c)))
    warn_duty = fan_min_duty + warn_ratio * (fan_max_duty - fan_min_duty)
    return _normalise_fan_control_points([
        {"temp_c": safe_max_c, "duty": fan_min_duty},
        {"temp_c": warn_max_c, "duty": warn_duty},
        {"temp_c": critical_c, "duty": fan_max_duty},
    ])


def _write_fan_control_points_to_config(points: list[dict[str, float]]) -> None:
    import yaml

    with open(_THERMAL_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    thresholds = cfg.setdefault("thresholds", {})
    thresholds["control_points"] = [{"temp_c": p["temp_c"], "duty": p["duty"]} for p in points]
    with open(_THERMAL_CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def _read_temp_blend_from_config() -> dict[str, float]:
    import yaml

    try:
        with open(_THERMAL_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        blend = cfg.get("temp_blend", {})
        cw = float(blend.get("case_weight", 0.2))
        pw = float(blend.get("cpu_weight",  0.8))
        total = cw + pw
        return {"case_weight": cw / total, "cpu_weight": pw / total}
    except Exception:
        return {"case_weight": 0.2, "cpu_weight": 0.8}


def _write_temp_blend_to_config(case_weight: float, cpu_weight: float) -> None:
    import yaml

    with open(_THERMAL_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}
    blend = cfg.setdefault("temp_blend", {})
    blend["case_weight"] = round(case_weight, 4)
    blend["cpu_weight"]  = round(cpu_weight, 4)
    with open(_THERMAL_CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


# ── Audio config helpers ──────────────────────────────────────────────────────

_AUDIO_BACKEND_DEFAULT       = "default"
_AUDIO_BACKEND_RESPEAKER     = "respeaker_flex"
_AUDIO_VALID_BACKENDS        = (_AUDIO_BACKEND_DEFAULT, _AUDIO_BACKEND_RESPEAKER)
_AUDIO_VALID_EQ_PRESETS      = ("flat", "bass_boost", "treble_boost", "vocal", "loudness", "warm", "custom")


def _read_audio_config() -> dict:
    """Return the full ``audio:`` section from assistant.yaml with defaults filled in."""
    import yaml

    try:
        with open(_ASSISTANT_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:
        cfg = {}

    audio = cfg.get("audio", {})

    default_defaults = {
        "input_device_name": "",
        "input_sample_rate": 44100,
        "output_alsa_device": "pulse",
        "output_sample_rate": 44100,
        "loudness_boost": 2.0,
        "eq_preset": "flat",
    }
    respeaker_defaults = {
        "input_device_name": "ReSpeaker",
        "input_sample_rate": 16000,
        "input_raw_channels": 6,
        "input_processed_channel": 0,
        "output_alsa_device": "pulse",
        "output_sample_rate": 44100,
        "loudness_boost": 2.0,
        "eq_preset": "flat",
        "led_enabled": True,
    }

    return {
        "backend": audio.get("backend", _AUDIO_BACKEND_DEFAULT),
        "default": {**default_defaults, **audio.get("default", {})},
        "respeaker_flex": {**respeaker_defaults, **audio.get("respeaker_flex", {})},
    }


def _write_audio_config(body: dict) -> None:
    """Merge *body* into the ``audio:`` section of assistant.yaml."""
    import yaml

    with open(_ASSISTANT_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    audio = cfg.setdefault("audio", {})

    if "backend" in body:
        audio["backend"] = str(body["backend"])

    for backend_key in ("default", "respeaker_flex"):
        if backend_key in body and isinstance(body[backend_key], dict):
            section = audio.setdefault(backend_key, {})
            section.update(body[backend_key])

    with open(_ASSISTANT_CONFIG_PATH, "w") as f:
        yaml.safe_dump(cfg, f, sort_keys=False)


def _list_audio_input_devices() -> list[dict]:
    """Return a list of available sounddevice input devices (best-effort)."""
    try:
        import sounddevice as sd
        devices = sd.query_devices()
        return [
            {"index": i, "name": d["name"], "channels": d.get("max_input_channels", 0)}
            for i, d in enumerate(devices)
            if d.get("max_input_channels", 0) > 0
        ]
    except Exception:
        return []


class WebService:
    """Async FastAPI server running in a background thread."""

    name = "web"

    def __init__(
        self,
        bus=None,
        host: str = "0.0.0.0",
        port: int = 8080,
        registry=None,
        vision_service=None,
        quiet_hours: Optional[QuietHours] = None,
        motion_service=None,
        tracking_service=None,
        music_service=None,
        camera2_service=None,
        object_service=None,
        skills_service=None,
        perception_service=None,
        dense_stereo_service=None,
        mono_depth_service=None,
        room_service=None,
        iot_registry=None,
        privacy_service=None,
        api_key: str = "",
    ) -> None:
        self.bus = bus
        self._host = host
        self._port = port
        self._api_key = api_key.strip() if api_key else ""
        self._registry = registry
        self._vision_svc = vision_service
        self._quiet_hours = quiet_hours
        self._motion_svc = motion_service
        self._tracking_svc = tracking_service
        self._music_svc = music_service
        self._camera2_svc = camera2_service
        self._object_svc = object_service
        self._skills_svc = skills_service
        self._perception_svc = perception_service
        self._dense_stereo_svc = dense_stereo_service
        self._mono_depth_svc = mono_depth_service
        self._room_svc = room_service
        self._iot_registry = iot_registry
        self._privacy_svc = privacy_service
        self._all_services: list = []  # seeded by core_main after list is built
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest_frame: Optional[bytes] = None   # JPEG bytes
        self._frame_event: Optional[asyncio.Event] = None   # created in _run_server
        self._latest_frame2: Optional[bytes] = None  # second camera JPEG
        self._frame2_event: Optional[asyncio.Event] = None  # created in _run_server
        self._ws_clients: list = []
        self._event_log: list[dict] = []             # recent bus events (capped)
        self._unsubs: list = []
        self._running = False
        self._service_states: dict = {}   # name -> "running" | "stopped" | "error"
        # FPS counters — incremented by _on_frame callbacks, sampled in _build_status_snapshot
        self._cam1_frame_count: int = 0
        self._cam2_frame_count: int = 0
        self._fps_tick_time: float = time.monotonic()
        # CPU/memory history — 60 samples (≈ 60 s at 1 Hz)
        self._cpu_history: collections.deque = collections.deque(maxlen=60)
        self._mem_history: collections.deque = collections.deque(maxlen=60)
        # Prime the non-blocking cpu_percent sampler so the first real read is accurate
        psutil.cpu_percent(interval=None)

    # ── Service lifecycle ─────────────────────────────────────────────

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._registry is None:
            try:
                from src.perception.face_registry import FaceRegistry
                self._registry = FaceRegistry()
            except Exception as exc:
                log.warning("FaceRegistry unavailable in WebService: %s", exc)

        # Subscribe to camera frames — cam1 uses jpeg_ready (set by encoder thread)
        # so the MJPEG stream doesn't block waiting for JPEG to be encoded.
        self._unsubs.append(
            self.bus.subscribe("vision.jpeg_ready", self._on_frame)
        )
        if self._camera2_svc is not None:
            self._unsubs.append(
                self.bus.subscribe("vision.frame2_ready", self._on_frame2)
            )
        # Seed status from services that already started before us.
        for svc in self._all_services:
            if svc is self:
                continue
            _name = getattr(svc, "name", None)
            if _name and hasattr(svc, "is_running") and svc.is_running():
                self._service_states[_name] = "running"

        # Subscribe to service lifecycle events for the Services panel
        self._unsubs.append(
            self.bus.subscribe("service.started", self._on_service_started)
        )
        self._unsubs.append(
            self.bus.subscribe("service.stopped", self._on_service_stopped)
        )
        # Subscribe to per-service error events so the panel can show red.
        _err_map = {
            "audio.error":       "audio_capture",
            "vision.error":      "vision",
            "perception.error":  "perception",
            "music.error":       "music",
            "thermal.error":     "telemetry",
        }
        for _topic, _svc_name in _err_map.items():
            _n = _svc_name  # capture for closure
            self._unsubs.append(
                self.bus.subscribe(_topic, lambda t, p, n=_n: self._on_service_error(n))
            )
        # Auto-recover from error state when "healthy" events arrive.
        _recovery_map = {
            "vision.jpeg_ready": "vision",
            "audio.chunk":       "audio_capture",
        }
        for _topic, _svc_name in _recovery_map.items():
            _n = _svc_name
            self._unsubs.append(
                self.bus.subscribe(_topic, lambda t, p, n=_n: self._on_service_recovered(n))
            )

        # Subscribe to all events for event log
        for topic in (
            "perception.faces", "face.identified", "av.spoke",
            "motion.position", "thermal.temp", "thermal.fan",
        ):
            self._unsubs.append(
                self.bus.subscribe(topic, lambda t, p, _t=topic: self._on_event(_t, p))
            )

        self._thread = threading.Thread(target=self._run_server, daemon=True, name="web-server")
        self._thread.start()
        self._running = True
        log.info("WebService started on http://%s:%d", self._host, self._port)
        if self.bus:
            self.bus.publish("service.started", {"name": self.name, "ts": time.time()})

    def stop(self) -> None:
        self._running = False
        for unsub in self._unsubs:
            try:
                unsub()
            except Exception:
                pass
        self._unsubs.clear()
        if self._loop and self._server:
            self._loop.call_soon_threadsafe(self._server.should_exit.__setattr__, 'value', True)
        if self._registry:
            try:
                self._registry.close()
            except Exception:
                pass
        if self.bus:
            self.bus.publish("service.stopped", {"name": self.name, "ts": time.time()})
        log.info("WebService stopped")

    # ── Internal bus handlers ─────────────────────────────────────────
    def _on_frame(self, _topic, payload) -> None:
        """Read the pre-encoded JPEG from VisionService and wake the MJPEG generator."""
        if self._vision_svc is None:
            return
        jpeg = self._vision_svc.latest_jpeg()
        if jpeg is not None:
            self._latest_frame = jpeg
            self._cam1_frame_count += 1
            if self._loop is not None and self._frame_event is not None:
                self._loop.call_soon_threadsafe(self._frame_event.set)

    def _on_frame2(self, _topic, payload) -> None:
        """Read JPEG from RawCameraService (second camera) and wake /stream2 generator."""
        if self._camera2_svc is None:
            return
        jpeg = self._camera2_svc.latest_jpeg()
        if jpeg is not None:
            self._latest_frame2 = jpeg
            self._cam2_frame_count += 1
            if self._loop is not None and self._frame2_event is not None:
                self._loop.call_soon_threadsafe(self._frame2_event.set)

    def _on_service_started(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "name" in payload:
            # Recovery — clear any prior error state.
            self._service_states[payload["name"]] = "running"

    def _on_service_stopped(self, _topic, payload) -> None:
        if isinstance(payload, dict) and "name" in payload:
            self._service_states[payload["name"]] = "stopped"

    def _on_service_error(self, service_name: str) -> None:
        """Mark a service degraded (red) when it publishes an error event."""
        # Only degrade if currently shown as running — don't override "stopped".
        if self._service_states.get(service_name) == "running":
            self._service_states[service_name] = "error"

    def _on_service_recovered(self, service_name: str) -> None:
        """Clear error state when a healthy-signal event arrives for a service."""
        if self._service_states.get(service_name) == "error":
            self._service_states[service_name] = "running"

    def _on_event(self, topic: str, payload) -> None:
        # Strip the heavy per-face array from perception events so the
        # WebSocket message stays small enough for the browser to handle.
        if topic == "perception.faces" and isinstance(payload, dict):
            payload = {"count": payload.get("count", 0),
                       "backend": payload.get("backend"),
                       "ts": payload.get("ts")}
        entry = {"topic": topic, "ts": time.time(), "payload": payload}
        self._event_log.append(entry)
        if len(self._event_log) > 100:
            self._event_log.pop(0)

    # ── Server bootstrap ──────────────────────────────────────────────

    def _run_server(self) -> None:
        import uvicorn
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        # asyncio.Events must be created inside the loop they belong to
        self._frame_event = asyncio.Event()
        self._frame2_event = asyncio.Event()
        app = self._build_app()
        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            loop="none",
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(config)
        self._loop.run_until_complete(self._server.serve())

    def _get_service_by_name(self, name: str):
        for svc in self._all_services:
            if getattr(svc, "name", None) == name:
                return svc
        return None

    def _build_status_snapshot(self) -> dict:
        """Pull latest status from the bus (same topics as IPC bridge)."""
        snapshot_topics = (
            "thermal.temp", "thermal.fan", "thermal.rpm",
            "motion.position",
            "vision.frame_ready", "vision.error",
            "audio.level",
            "perception.faces", "face.identified",
            "perception.objects",
            "av.spoke",
        )
        last = {}
        for t in snapshot_topics:
            try:
                last[t] = self.bus.last(t) if self.bus else None
            except Exception:
                last[t] = None

        # Strip per-face detail from perception.faces so the WS payload stays small,
        # but keep bbox for the browser overlay.
        pf = last.get("perception.faces")
        if isinstance(pf, dict) and "faces" in pf:
            last["perception.faces"] = {
                "count":   pf.get("count", 0),
                "backend": pf.get("backend"),
                "ts":      pf.get("ts"),
                "faces": [
                    {"name": f.get("name"), "face_id": f.get("face_id"),
                     "bbox": f.get("bbox"), "centroid": f.get("centroid"),
                     "confidence": f.get("confidence")}
                    for f in (pf.get("faces") or [])
                ],
            }

        # Include frame dimensions so the browser can scale bbox coords.
        vfr = last.get("vision.frame_ready")
        if isinstance(vfr, dict) and vfr.get("shape"):
            shape = vfr["shape"]  # (H, W, C)
            last["vision.frame_ready"] = {"frame_w": shape[1], "frame_h": shape[0]}

        from src.core.version import get_version
        motion_pos = last.get("motion.position")
        servo_angle = float(motion_pos["angle"]) if isinstance(motion_pos, dict) and "angle" in motion_pos else None

        # Compute per-camera fps from frame counters since last snapshot call.
        now = time.monotonic()
        elapsed = now - self._fps_tick_time
        if elapsed > 0:
            cam1_fps = round(self._cam1_frame_count / elapsed, 1)
            cam2_fps = round(self._cam2_frame_count / elapsed, 1)
        else:
            cam1_fps = 0.0
            cam2_fps = 0.0
        self._cam1_frame_count = 0
        self._cam2_frame_count = 0
        self._fps_tick_time = now

        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        self._cpu_history.append(round(cpu, 1))
        self._mem_history.append(round(mem, 1))

        return {
            "version": get_version(),
            "ts": time.time(),
            "last": last,
            "services": dict(self._service_states),
            "servo_angle": servo_angle,
            "cam1_fps": cam1_fps,
            "cam2_fps": cam2_fps,
            "cpu_percent": cpu,
            "mem_percent": mem,
            "cpu_history": list(self._cpu_history),
            "mem_history": list(self._mem_history),
            "room": self._room_svc.room_name if self._room_svc else None,
            "room_detail": self._room_svc.get_status_dict() if self._room_svc else None,
            "iot": self._iot_registry.get_all_snapshots() if self._iot_registry else {},
        }

    # ── FastAPI app ───────────────────────────────────────────────────

    def _build_app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
        from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles
        from starlette.middleware.base import BaseHTTPMiddleware

        app = FastAPI(title="VERA Dashboard", docs_url=None, redoc_url=None)

        # ── API key authentication ────────────────────────────────────
        # All routes except /, /health, and /static/* require the key
        # either as an X-API-Key header or a ?key= query parameter.
        _api_key = self._api_key

        if _api_key:
            class _AuthMiddleware(BaseHTTPMiddleware):
                async def dispatch(self, request, call_next):
                    path = request.url.path
                    # Always-public routes (no key required)
                    if (
                        path in ("/", "/health")
                        or path.startswith("/static")
                    ):
                        return await call_next(request)
                    key = (
                        request.headers.get("x-api-key")
                        or request.query_params.get("key", "")
                    )
                    if key != _api_key:
                        return JSONResponse({"error": "Unauthorized"}, status_code=401)
                    return await call_next(request)
            app.add_middleware(_AuthMiddleware)
        else:
            import logging as _log
            _log.getLogger(__name__).warning(
                "VERA_API_KEY is not set — web dashboard is UNAUTHENTICATED"
            )

        # Serve static files (CSS, JS)
        if _STATIC_DIR.exists():
            app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

        # ── Dashboard HTML ────────────────────────────────────────────

        @app.get("/", response_class=HTMLResponse)
        async def index():
            html_path = _STATIC_DIR / "index.html"
            return HTMLResponse(content=html_path.read_text(), status_code=200)

        # ── MJPEG stream ──────────────────────────────────────────────

        @app.get("/stream")
        async def mjpeg_stream():
            async def generate():
                last_sent: Optional[bytes] = None
                while True:
                    evt = self._frame_event
                    if evt is None:
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    evt.clear()
                    frame = self._latest_frame
                    if frame and frame is not last_sent:
                        last_sent = frame
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )

            return StreamingResponse(
                generate(),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        @app.get("/stream2")
        async def mjpeg_stream2():
            async def generate2():
                last_sent: Optional[bytes] = None
                while True:
                    evt = self._frame2_event
                    if evt is None:
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        await asyncio.wait_for(evt.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    evt.clear()
                    frame = self._latest_frame2
                    if frame and frame is not last_sent:
                        last_sent = frame
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )

            return StreamingResponse(
                generate2(),
                media_type="multipart/x-mixed-replace; boundary=frame",
            )

        # ── WebSocket ─────────────────────────────────────────────────

        @app.websocket("/ws")
        async def websocket_endpoint(ws: WebSocket):
            await ws.accept()
            if _api_key and ws.query_params.get("key") != _api_key:
                await ws.close(code=1008)
                return
            self._ws_clients.append(ws)
            try:
                while True:
                    snapshot = self._build_status_snapshot()
                    snapshot["events"] = self._event_log[-20:]
                    await ws.send_text(json.dumps(snapshot, default=str))
                    await asyncio.sleep(1.0)
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                if ws in self._ws_clients:
                    self._ws_clients.remove(ws)

        # ── REST: status ──────────────────────────────────────────────

        @app.get("/health")
        async def health():
            return JSONResponse({"ok": True, "status": "live"})

        @app.get("/api/status")
        async def api_status():
            return JSONResponse(self._build_status_snapshot())

        # ── REST: faces ───────────────────────────────────────────────

        @app.get("/api/faces")
        async def api_faces():
            if not self._registry:
                return JSONResponse({"faces": []})
            faces = self._registry.list_faces()
            # Annotate each face with whether a thumbnail is available
            for f in faces:
                f["has_thumb"] = self._registry.thumbnail_path(f["id"]) is not None
            return JSONResponse({"faces": faces})

        @app.get("/api/faces/{face_id}/thumb")
        async def api_face_thumb(face_id: str):
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            path = self._registry.thumbnail_path(face_id)
            if path is None:
                raise HTTPException(404, "thumbnail not found")
            from fastapi.responses import FileResponse
            return FileResponse(str(path), media_type="image/jpeg")

        @app.get("/api/faces/{face_id}/photo")
        async def api_face_photo(face_id: str):
            """Full-size face photo. Falls back to thumbnail if no photo stored."""
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            path = self._registry.photo_path(face_id) or self._registry.thumbnail_path(face_id)
            if path is None:
                raise HTTPException(404, "photo not found")
            from fastapi.responses import FileResponse
            return FileResponse(str(path), media_type="image/jpeg")

        @app.delete("/api/faces")
        async def api_delete_all_faces():
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            count = self._registry.delete_all_faces()
            if self.bus:
                self.bus.publish("face.registry_cleared", {"count": count})
            return {"ok": True, "deleted": count}

        @app.delete("/api/faces/guests")
        async def api_delete_guest_faces():
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            count, deleted_ids = self._registry.delete_guest_faces()
            if self.bus:
                self.bus.publish("face.guests_cleared", {"count": count, "face_ids": deleted_ids})
            return {"ok": True, "deleted": count}

        @app.post("/api/faces/merge")
        async def api_merge_faces(body: _MergeFacesBody):
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            ok = self._registry.merge_faces(body.keep_id, body.absorb_id)
            if not ok:
                raise HTTPException(404, "one or both face IDs not found")
            if self.bus:
                self.bus.publish("face.merged", {"keep_id": body.keep_id, "absorb_id": body.absorb_id})
            return {"ok": True}

        @app.post("/api/faces/refresh")
        async def api_refresh_faces():
            pruned = 0
            if self._registry:
                pruned = self._registry.prune_gallery()
            if self.bus:
                self.bus.publish("face.refresh", {})
            faces = self._registry.list_faces() if self._registry else []
            return {"ok": True, "count": len(faces), "pruned": pruned}

        # ── Quiet-hours settings ───────────────────────────────────────

        @app.get("/api/settings/quiet-hours")
        async def api_get_quiet_hours():
            if self._quiet_hours is None:
                return {"enabled": False, "start": "21:00", "end": "06:00"}
            return self._quiet_hours.as_dict()

        @app.put("/api/settings/quiet-hours")
        async def api_put_quiet_hours(body: _QuietHoursBody):
            if self._quiet_hours is None:
                raise HTTPException(503, "quiet hours not configured")
            try:
                self._quiet_hours.update(body.enabled, body.start, body.end)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            if self.bus:
                self.bus.publish("settings.quiet_hours_updated", self._quiet_hours.as_dict())
            return {"ok": True, **self._quiet_hours.as_dict()}

        # ── Room ──────────────────────────────────────────────────────

        @app.get("/api/room")
        async def api_get_room():
            name = self._room_svc.room_name if self._room_svc else None
            return {"name": name}

        @app.get("/api/room/status")
        async def api_room_status():
            if not self._room_svc:
                raise HTTPException(503, "room service unavailable")
            return self._room_svc.get_status_dict()

        @app.put("/api/room")
        async def api_set_room(body: _RoomBody):
            if self.bus:
                self.bus.publish("room.set", {"name": body.name})
            return {"ok": True, "name": body.name}

        @app.put("/api/faces/{face_id}")
        async def api_rename_face(face_id: str, body: _RenameBody):
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            ok = self._registry.set_name(face_id, body.name)
            if not ok:
                raise HTTPException(404, "face not found")
            if self.bus:
                self.bus.publish("face.meet", {"name": body.name, "face_id": face_id})
            return {"ok": True}

        @app.post("/api/faces/{face_id}/train")
        async def api_train_face(face_id: str):
            """Capture the current camera frame and add an embedding/thumbnail for face_id."""
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            if not self._perception_svc:
                raise HTTPException(503, "perception service unavailable")
            import asyncio
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, self._perception_svc.capture_training_image, face_id
            )
            if not result.get("ok"):
                reason = result.get("reason", "unknown")
                code = 404 if reason == "face_not_found" else 503
                raise HTTPException(code, reason)
            if self.bus:
                self.bus.publish("face.training_capture", {"face_id": face_id, **result})
            return result

        @app.delete("/api/faces/{face_id}")
        async def api_delete_face(face_id: str):
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            ok = self._registry.delete_face(face_id)
            if not ok:
                raise HTTPException(404, "face not found")
            if self.bus:
                self.bus.publish("face.deleted", {"face_id": face_id})
            return {"ok": True}

        # ── Servo settings ─────────────────────────────────────────────

        @app.get("/api/settings/servo")
        async def api_get_servo():
            enabled = self._motion_svc.servo_enabled if self._motion_svc else True
            return {"enabled": enabled}

        @app.put("/api/settings/servo")
        async def api_put_servo(body: _ServoBody):
            if self.bus:
                self.bus.publish("motion.set_enabled", {"enabled": body.enabled})
            return {"ok": True, "enabled": body.enabled}

        @app.get("/api/settings/servo/limits")
        async def api_get_servo_limits():
            if self._motion_svc:
                mn = self._motion_svc.soft_min_deg
                mx = self._motion_svc.soft_max_deg
            else:
                mn, mx = 135.0, 215.0
            return {"min_deg": mn, "max_deg": mx}

        @app.put("/api/settings/servo/limits")
        async def api_put_servo_limits(body: _ServoLimitsBody):
            if body.min_deg < 1 or body.max_deg > 360 or body.min_deg >= body.max_deg:
                raise HTTPException(422, "min_deg must be >= 1, max_deg <= 360, and min < max")
            if self.bus:
                self.bus.publish(
                    "motion.set_limits",
                    {"min_deg": body.min_deg, "max_deg": body.max_deg},
                )
            return {"ok": True, "min_deg": body.min_deg, "max_deg": body.max_deg}

        @app.get("/api/settings/face-tracking")
        async def api_get_face_tracking():
            enabled = self._tracking_svc.face_tracking_enabled if self._tracking_svc else True
            return {"enabled": enabled}

        @app.put("/api/settings/face-tracking")
        async def api_put_face_tracking(body: _ServoBody):
            if self.bus:
                self.bus.publish("tracking.set_face_tracking", {"enabled": body.enabled})
            return {"ok": True, "enabled": body.enabled}

        @app.get("/api/settings/random-motion")
        async def api_get_random_motion():
            enabled = self._tracking_svc.random_motion_enabled if self._tracking_svc else True
            return {"enabled": enabled}

        @app.put("/api/settings/random-motion")
        async def api_put_random_motion(body: _ServoBody):
            if self.bus:
                self.bus.publish("tracking.set_random_motion", {"enabled": body.enabled})
            return {"ok": True, "enabled": body.enabled}

        # ── REST: head-tracking tuning ────────────────────────────────

        @app.get("/api/tracking/params")
        async def api_get_tracking_params():
            if not self._tracking_svc:
                return JSONResponse({"params": {}, "ranges": {}, "presets": []})
            return JSONResponse(self._tracking_svc.get_tunable_params())

        @app.post("/api/tracking/params")
        async def api_post_tracking_param(body: _TrackingParamBody):
            ok = bool(self._tracking_svc and self._tracking_svc.set_tunable_param(body.name, body.value))
            return {"ok": ok, "name": body.name, "value": body.value}

        @app.post("/api/tracking/save")
        async def api_post_tracking_save():
            if self.bus:
                self.bus.publish("tracking.save_params", {})
            return {"ok": True}

        @app.post("/api/tracking/preset")
        async def api_post_tracking_preset(body: _TrackingPresetBody):
            if self.bus:
                self.bus.publish("tracking.apply_preset", {"name": body.name})
            return {"ok": True, "name": body.name}

        @app.post("/api/tracking/reset")
        async def api_post_tracking_reset():
            if self.bus:
                self.bus.publish("tracking.reset_params", {})
            return {"ok": True}

        @app.post("/api/tracking/autotune/start")
        async def api_post_tracking_autotune_start():
            if self.bus:
                self.bus.publish("tracking.start_autotune", {})
            return {"ok": True}

        @app.post("/api/tracking/autotune/cancel")
        async def api_post_tracking_autotune_cancel():
            if self.bus:
                self.bus.publish("tracking.cancel_autotune", {})
            return {"ok": True}

        @app.websocket("/ws/tracking-debug")
        async def ws_tracking_debug(ws: WebSocket):
            """Live stream of tracking.debug + autotune events at ~10 Hz."""
            await ws.accept()
            if _api_key and ws.query_params.get("key") != _api_key:
                await ws.close(code=1008)
                return
            queue: asyncio.Queue = asyncio.Queue(maxsize=128)
            loop = asyncio.get_event_loop()

            def _on_event(_topic, payload):
                try:
                    loop.call_soon_threadsafe(queue.put_nowait, (_topic, payload))
                except Exception:
                    pass

            unsubs = []
            if self.bus:
                unsubs.append(self.bus.subscribe("tracking.debug", _on_event))
                unsubs.append(self.bus.subscribe("tracking.autotune_progress", _on_event))
                unsubs.append(self.bus.subscribe("tracking.autotune_done", _on_event))
                unsubs.append(self.bus.subscribe("tracking.param_changed", _on_event))
                unsubs.append(self.bus.subscribe("tracking.preset_applied", _on_event))
                unsubs.append(self.bus.subscribe("tracking.save_params_done", _on_event))
            try:
                while True:
                    topic, payload = await queue.get()
                    await ws.send_text(json.dumps({"topic": topic, "payload": payload}, default=str))
            except (WebSocketDisconnect, Exception):
                pass
            finally:
                for u in unsubs:
                    try: u()
                    except Exception: pass

        @app.get("/api/settings/object-detection")
        async def api_get_object_detection():
            enabled = self._object_svc.detection_enabled if self._object_svc else True
            return {"enabled": enabled}

        @app.put("/api/settings/object-detection")
        async def api_put_object_detection(body: _ServoBody):
            if self.bus:
                self.bus.publish("object.set_enabled", {"enabled": body.enabled})
            return {"ok": True, "enabled": body.enabled}

        @app.get("/api/settings/fan/control-points")
        async def api_get_fan_control_points():
            thermal = _thermal_request({"cmd": "fan_control_points.get"})
            if thermal.get("ok") and isinstance(thermal.get("control_points"), list):
                try:
                    runtime_points = _normalise_fan_control_points(thermal["control_points"])
                    return {
                        "ok": True,
                        "control_points": runtime_points,
                        "runtime_source": "thermal",
                    }
                except ValueError:
                    pass
            try:
                points = _read_fan_control_points_from_config()
            except Exception as exc:
                raise HTTPException(500, f"Unable to read fan control points: {exc}")
            return {
                "ok": True,
                "control_points": points,
                "runtime_source": "config",
                "runtime_error": thermal.get("error"),
            }

        @app.put("/api/settings/fan/control-points")
        async def api_put_fan_control_points(body: _FanControlPointsBody):
            try:
                raw_points = [p.model_dump() if hasattr(p, "model_dump") else p.dict() for p in body.points]
                points = _normalise_fan_control_points(raw_points)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            try:
                _write_fan_control_points_to_config(points)
            except Exception as exc:
                raise HTTPException(500, f"Unable to save fan control points: {exc}")

            thermal = _thermal_request({"cmd": "fan_control_points.set", "points": points})
            if thermal.get("ok"):
                return {
                    "ok": True,
                    "control_points": _normalise_fan_control_points(thermal.get("control_points") or points),
                    "runtime_applied": True,
                }
            return {
                "ok": True,
                "control_points": points,
                "runtime_applied": False,
                "runtime_error": thermal.get("error"),
            }

        @app.get("/api/settings/fan/temp-blend")
        async def api_get_temp_blend():
            thermal = _thermal_request({"cmd": "temp_blend.get"})
            if thermal.get("ok"):
                return {
                    "ok": True,
                    "case_weight": thermal["case_weight"],
                    "cpu_weight":  thermal["cpu_weight"],
                    "runtime_source": "runtime",
                }
            cfg_blend = _read_temp_blend_from_config()
            return {"ok": True, **cfg_blend, "runtime_source": "config",
                    "runtime_error": thermal.get("error")}

        @app.put("/api/settings/fan/temp-blend")
        async def api_put_temp_blend(body: _TempBlendBody):
            total = body.case_weight + body.cpu_weight
            if total <= 0:
                raise HTTPException(422, "case_weight + cpu_weight must be > 0")
            cw = body.case_weight / total
            pw = body.cpu_weight  / total
            try:
                _write_temp_blend_to_config(cw, pw)
            except Exception as exc:
                raise HTTPException(500, f"Unable to save temp blend: {exc}")
            thermal = _thermal_request({"cmd": "temp_blend.set",
                                        "case_weight": cw, "cpu_weight": pw})
            if thermal.get("ok"):
                return {"ok": True, "case_weight": thermal["case_weight"],
                        "cpu_weight": thermal["cpu_weight"], "runtime_applied": True}
            return {"ok": True, "case_weight": cw, "cpu_weight": pw,
                    "runtime_applied": False, "runtime_error": thermal.get("error")}

        # ── REST: audio backend settings ─────────────────────────────

        @app.get("/api/settings/audio")
        async def api_get_audio():
            try:
                audio = _read_audio_config()
            except Exception as exc:
                raise HTTPException(500, f"Unable to read audio config: {exc}")
            devices = _list_audio_input_devices()
            return {
                "ok": True,
                "backend": audio["backend"],
                "default": audio["default"],
                "respeaker_flex": audio["respeaker_flex"],
                "available_backends": list(_AUDIO_VALID_BACKENDS),
                "available_eq_presets": list(_AUDIO_VALID_EQ_PRESETS),
                "available_input_devices": devices,
            }

        @app.put("/api/settings/audio")
        async def api_put_audio(body: _AudioSettingsBody):
            if body.backend not in _AUDIO_VALID_BACKENDS:
                raise HTTPException(
                    422,
                    f"Unknown backend {body.backend!r}. "
                    f"Valid: {list(_AUDIO_VALID_BACKENDS)}",
                )
            patch: dict = {"backend": body.backend}
            if body.default is not None:
                patch["default"] = body.default.model_dump()
            if body.respeaker_flex is not None:
                patch["respeaker_flex"] = body.respeaker_flex.model_dump()
            try:
                _write_audio_config(patch)
            except Exception as exc:
                raise HTTPException(500, f"Unable to save audio config: {exc}")
            updated = _read_audio_config()
            return {
                "ok": True,
                "backend": updated["backend"],
                "default": updated["default"],
                "respeaker_flex": updated["respeaker_flex"],
                "restart_required": True,
                "message": "Audio backend change takes effect after service restart.",
            }

        # ── REST: greeting settings ───────────────────────────────────

        @app.get("/api/settings/greeting")
        async def api_get_greeting():
            import yaml
            cfg_path = Path(__file__).resolve().parent.parent.parent / "config" / "assistant.yaml"
            cfg: dict = {}
            try:
                with open(cfg_path) as f:
                    cfg = yaml.safe_load(f) or {}
            except Exception:
                pass
            fr = cfg.get("face_recognition", {})
            return {
                "cooldown_min":         fr.get("greeting_cooldown_min", 30.0),
                "jitter_pct":           fr.get("greeting_cooldown_jitter_pct", 25.0),
                "min_absence_s":        fr.get("min_absence_s", 30.0),
                "confidence_threshold": fr.get("confidence_threshold", 0.5),
                "enabled":              fr.get("enabled", True),
            }

        @app.put("/api/settings/greeting")
        async def api_put_greeting(body: _GreetingBody):
            if self.bus:
                payload: dict = {"cooldown_min": body.cooldown_min}
                if body.jitter_pct is not None:
                    payload["jitter_pct"] = body.jitter_pct
                if body.min_absence_s is not None:
                    payload["min_absence_s"] = body.min_absence_s
                if body.confidence_threshold is not None:
                    payload["confidence_threshold"] = body.confidence_threshold
                self.bus.publish("tracking.set_greeting_cooldown", payload)
            return {"ok": True, **{k: v for k, v in {
                "cooldown_min": body.cooldown_min,
                "jitter_pct": body.jitter_pct,
                "min_absence_s": body.min_absence_s,
                "confidence_threshold": body.confidence_threshold,
            }.items() if v is not None}}


        @app.post("/api/say")
        async def api_say(body: _SayBody):
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.say", {"text": body.text})
            return {"ok": True}

        @app.post("/api/audio/record")
        async def api_audio_record(body: _RecordBody):
            av_svc = self._get_service_by_name("av")
            if av_svc is None or not hasattr(av_svc, "record_clip"):
                raise HTTPException(503, "av service unavailable")
            try:
                # record_clip blocks on queue.Queue.get() — run off the event loop
                return await asyncio.to_thread(
                    av_svc.record_clip, seconds=body.seconds, path=body.path
                )
            except Exception as exc:
                raise HTTPException(500, f"record failed: {exc}")

        @app.post("/api/audio/playback")
        async def api_audio_playback(body: _PlaybackBody):
            av_svc = self._get_service_by_name("av")
            if av_svc is None or not hasattr(av_svc, "play_recording"):
                raise HTTPException(503, "av service unavailable")
            try:
                # play_recording also blocks — run off the event loop
                return await asyncio.to_thread(av_svc.play_recording, path=body.path)
            except FileNotFoundError as exc:
                raise HTTPException(404, str(exc))
            except Exception as exc:
                raise HTTPException(500, f"playback failed: {exc}")

        # ── Skills ────────────────────────────────────────────────────

        @app.get("/api/skills")
        async def api_skills():
            if self._skills_svc is None:
                return JSONResponse({"skills": []})
            skills_info = []
            for skill in self._skills_svc.registry.skills:
                patterns = skill.patterns
                example = ""
                if patterns:
                    raw = patterns[0].pattern
                    example = (raw
                               .replace(r"\b", "").replace(r"(", "").replace(r")", "")
                               .replace(r"[", "").replace(r"]", "")
                               .replace("?", "").replace("+", "").replace("*", "")
                               .replace("\\", "").strip())
                schema = skill.config_schema
                config_values = skill.get_config() if schema else {}
                skills_info.append({
                    "name":          skill.name,
                    "enabled":       skill.enabled,
                    "example":       example,
                    "pattern_count": len(patterns),
                    "has_config":    bool(schema),
                    "config_schema": [f.as_dict() for f in schema],
                    "config_values": config_values,
                })
            return JSONResponse({"skills": skills_info})

        @app.post("/api/skills/{skill_name}/enabled")
        async def api_skill_enabled(skill_name: str, body: _SkillEnabledBody):
            if self._skills_svc is None:
                raise HTTPException(503, "skills unavailable")
            skill = self._skills_svc.find_skill(skill_name)
            if skill is None:
                raise HTTPException(404, f"Skill {skill_name!r} not found")
            skill.enabled = body.enabled
            return {"ok": True, "name": skill_name, "enabled": skill.enabled}

        @app.get("/api/skills/{skill_name}/config")
        async def api_skill_config_get(skill_name: str):
            if self._skills_svc is None:
                raise HTTPException(503, "skills unavailable")
            skill = self._skills_svc.find_skill(skill_name)
            if skill is None:
                raise HTTPException(404, f"Skill {skill_name!r} not found")
            return JSONResponse({
                "name":   skill_name,
                "schema": [f.as_dict() for f in skill.config_schema],
                "values": skill.get_config(),
            })

        @app.post("/api/skills/{skill_name}/config")
        async def api_skill_config_set(skill_name: str, body: _SkillConfigBody):
            if self._skills_svc is None:
                raise HTTPException(503, "skills unavailable")
            skill = self._skills_svc.find_skill(skill_name)
            if skill is None:
                raise HTTPException(404, f"Skill {skill_name!r} not found")
            try:
                skill.set_config(body.key, body.value)
            except ValueError as exc:
                raise HTTPException(400, str(exc))
            return {"ok": True, "name": skill_name, "key": body.key, "value": body.value}

        @app.post("/api/utterance")
        async def api_utterance(body: _SayBody):
            """Dispatch text as a voice utterance to the skills engine."""
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.utterance", {"text": body.text})
            return {"ok": True}


        @app.post("/api/pan")
        async def api_pan(body: _PanBody):
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("motion.pan_to", {"angle": body.angle, "override_quiet": True})
            return {"ok": True}

        @app.get("/api/snapshot")
        async def api_snapshot():
            """Return the current camera 1 frame as a full-resolution JPEG."""
            import cv2
            svc = self._vision_svc
            if svc is None:
                raise HTTPException(503, "vision service unavailable")
            frame = svc.latest_frame()
            if frame is None:
                raise HTTPException(503, "no frame available")
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise HTTPException(500, "JPEG encode failed")
            from fastapi.responses import Response
            return Response(content=bytes(buf), media_type="image/jpeg")

        @app.get("/api/snapshot2")
        async def api_snapshot2():
            """Return the current camera 2 frame as a full-resolution JPEG."""
            import cv2
            svc = self._camera2_svc
            if svc is None:
                raise HTTPException(503, "camera 2 not enabled")
            frame = svc.latest_frame()
            if frame is None:
                raise HTTPException(503, "no frame available")
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                raise HTTPException(500, "JPEG encode failed")
            from fastapi.responses import Response
            return Response(content=bytes(buf), media_type="image/jpeg")

        @app.post("/api/version")
        async def api_version():
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.announce_version", None)
            return {"ok": True}

        @app.post("/api/joke")
        async def api_joke():
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.tell_joke", None)
            return {"ok": True}

        @app.post("/api/time")
        async def api_time():
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.announce_time", None)
            return {"ok": True}

        @app.post("/api/restart")
        async def api_restart():
            import asyncio
            import subprocess
            import os

            async def _do_restart():
                await asyncio.sleep(0.4)
                # subprocess.Popen from within a systemd service runs in the service's
                # cgroup.  When systemd kills the cgroup to restart the unit it also
                # kills any child subprocess before the restart can be registered.
                #
                # Fix: use `systemd-run --user` to spawn the restart command in a
                # transient user service (its own cgroup, outside the daemon's cgroup).
                # `DBUS_SESSION_BUS_ADDRESS` must be set explicitly — system services
                # don't inherit it from the user session.
                uid = os.getuid()
                env = os.environ.copy()
                env["XDG_RUNTIME_DIR"] = f"/run/user/{uid}"
                env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path=/run/user/{uid}/bus"
                subprocess.Popen(
                    [
                        "systemd-run", "--user", "--no-block", "--collect",
                        "/bin/sh", "-c",
                        "sleep 0.3 && sudo /usr/bin/systemctl restart "
                        "desktop-assistant-core.service",
                    ],
                    close_fds=True,
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )

            asyncio.ensure_future(_do_restart())
            return {"ok": True, "message": "Restarting daemon…"}

        @app.post("/api/system/reboot")
        async def api_reboot():
            import asyncio
            import subprocess

            async def _do_reboot():
                # Center the head; MotionService.on_stop() also centers but
                # publishing here gives a 1 s head-start before the process exits.
                if self.bus:
                    self.bus.publish("motion.pan_to", {"angle": 180.0})
                await asyncio.sleep(1.2)
                subprocess.Popen(["sudo", "reboot"], close_fds=True)

            asyncio.ensure_future(_do_reboot())
            return {"ok": True, "message": "Rebooting system…"}

        @app.post("/api/system/shutdown")
        async def api_shutdown():
            import asyncio
            import subprocess

            async def _do_shutdown():
                if self.bus:
                    self.bus.publish("motion.pan_to", {"angle": 180.0})
                await asyncio.sleep(1.2)
                subprocess.Popen(["sudo", "shutdown", "-h", "now"], close_fds=True)

            asyncio.ensure_future(_do_shutdown())
            return {"ok": True, "message": "Shutting down…"}

        @app.post("/api/vision/describe")
        async def api_vision_describe():
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            faces_payload = self.bus.last("perception.faces")
            objs_payload = self.bus.last("perception.objects")
            description = _build_scene_description(faces_payload, objs_payload)
            self.bus.publish("av.say", {"text": description})
            return {"ok": True, "description": description}

        # ── Camera rotation ────────────────────────────────────────────

        @app.get("/api/settings/camera/rotation")
        async def api_get_camera_rotation():
            deg = self._vision_svc.rotation_deg if self._vision_svc else 0
            return {"rotation_deg": deg}

        @app.put("/api/settings/camera/rotation")
        async def api_put_camera_rotation(body: _CameraRotationBody):
            deg = int(body.rotation_deg) % 360
            if self.bus:
                self.bus.publish("camera.set_rotation", {"rotation_deg": deg})
            return {"ok": True, "rotation_deg": deg}

        # ── Camera 2 rotation ──────────────────────────────────────────

        @app.get("/api/settings/camera2/rotation")
        async def api_get_camera2_rotation():
            deg = self._camera2_svc.rotation_deg if self._camera2_svc else 0
            return {"rotation_deg": deg}

        @app.put("/api/settings/camera2/rotation")
        async def api_put_camera2_rotation(body: _CameraRotationBody):
            deg = int(body.rotation_deg) % 360
            if self.bus:
                self.bus.publish("camera2.set_rotation", {"rotation_deg": deg})
            return {"ok": True, "rotation_deg": deg}

        # ── Camera resolution (both cameras) ──────────────────────────────

        @app.get("/api/settings/camera/resolution")
        async def api_get_camera_resolution():
            if self._vision_svc:
                w, h = self._vision_svc.resolution
            else:
                w, h = 640, 480
            return {"width": w, "height": h}

        @app.put("/api/settings/camera/resolution")
        async def api_put_camera_resolution(body: _CameraResolutionBody):
            if self.bus:
                self.bus.publish("camera.set_resolution", {"width": body.width, "height": body.height})
            return {"ok": True, "width": body.width, "height": body.height}

        # ── Stream resolution (MJPEG downscale — no camera restart) ───────

        @app.get("/api/settings/camera/stream_resolution")
        async def api_get_stream_resolution():
            if self._vision_svc:
                w, h = self._vision_svc.stream_resolution
            else:
                w, h = 640, 480
            return {"width": w if w > 0 else 640, "height": h if h > 0 else 480}

        @app.put("/api/settings/camera/stream_resolution")
        async def api_put_stream_resolution(body: _CameraResolutionBody):
            if self.bus:
                self.bus.publish("camera.set_stream_resolution",
                                 {"width": body.width, "height": body.height})
            return {"ok": True, "width": body.width, "height": body.height}

        # ── Camera 2 capture + stream resolution ────────────────────────

        @app.get("/api/settings/camera2/resolution")
        async def api_get_camera2_resolution():
            if self._camera2_svc:
                w, h = self._camera2_svc.resolution
            else:
                w, h = 640, 480
            return {"width": w, "height": h}

        @app.put("/api/settings/camera2/resolution")
        async def api_put_camera2_resolution(body: _CameraResolutionBody):
            if self.bus:
                self.bus.publish("camera2.set_resolution",
                                 {"width": body.width, "height": body.height})
            return {"ok": True, "width": body.width, "height": body.height}

        @app.get("/api/settings/camera2/stream_resolution")
        async def api_get_camera2_stream_resolution():
            if self._camera2_svc:
                w, h = self._camera2_svc.stream_resolution
                if w == 0:
                    w, h = self._camera2_svc.resolution  # 0 means use full capture res
            else:
                w, h = 640, 480
            return {"width": w, "height": h}

        @app.put("/api/settings/camera2/stream_resolution")
        async def api_put_camera2_stream_resolution(body: _CameraResolutionBody):
            if self.bus:
                self.bus.publish("camera2.set_stream_resolution",
                                 {"width": body.width, "height": body.height})
            return {"ok": True, "width": body.width, "height": body.height}

        # ── Depth settings ──────────────────────────────────────────────

        @app.get("/api/settings/depth")
        async def api_get_depth_settings():
            last = self.bus.last("vision.depth_map") if self.bus else None
            mono_last = self.bus.last("vision.mono_depth_map") if self.bus else None
            return JSONResponse({
                "ok": True,
                "dense_enabled": getattr(self._dense_stereo_svc, "_enabled", False),
                "mono_enabled": getattr(self._mono_depth_svc, "_enabled", False),
                "calibrated": last.get("calibrated", False) if last else False,
                "mono_hardware_ready": mono_last.get("hardware_ready", False) if mono_last else (
                    getattr(self._mono_depth_svc, "hardware_ready", False)
                    if self._mono_depth_svc else False
                ),
            })

        @app.put("/api/settings/depth")
        async def api_put_depth_settings(body: dict):
            if self.bus:
                if "dense_enabled" in body:
                    self.bus.publish("depth.set_dense_enabled", {"enabled": bool(body["dense_enabled"])})
                if "mono_enabled" in body:
                    self.bus.publish("depth.set_mono_enabled", {"enabled": bool(body["mono_enabled"])})
            # Persist to config/assistant.yaml so settings survive restarts.
            try:
                import yaml as _yaml
                _cfg_path = _ASSISTANT_CONFIG_PATH
                with open(_cfg_path) as _f:
                    _cfg = _yaml.safe_load(_f) or {}
                if "depth" not in _cfg:
                    _cfg["depth"] = {}
                if "dense_enabled" in body:
                    _cfg["depth"]["dense_enabled"] = bool(body["dense_enabled"])
                if "mono_enabled" in body:
                    _cfg["depth"]["mono_enabled"] = bool(body["mono_enabled"])
                with open(_cfg_path, "w") as _f:
                    _yaml.dump(_cfg, _f, default_flow_style=False, allow_unicode=True)
            except Exception as _exc:
                log.warning("depth settings: could not persist to YAML: %s", _exc)
            return {"ok": True}

        # ── Privacy settings ─────────────────────────────────────────────

        @app.get("/api/settings/privacy")
        async def api_get_privacy_settings():
            svc = self._privacy_svc
            return {
                "enabled":            getattr(svc, "_enabled", True) if svc else True,
                "hardware_ready":     getattr(svc, "hardware_ready", False) if svc else False,
                "rate_hz":            getattr(getattr(svc, "_cfg", None), "rate_hz", 1.0),
                "threshold":          getattr(getattr(svc, "_cfg", None), "threshold", 0.6),
                "look_away_angle_deg":getattr(getattr(svc, "_cfg", None), "look_away_angle_deg", 45.0),
                "cooldown_s":         getattr(getattr(svc, "_cfg", None), "cooldown_s", 10.0),
                "announce":           getattr(getattr(svc, "_cfg", None), "announce", True),
            }

        @app.put("/api/settings/privacy")
        async def api_put_privacy_settings(body: dict):
            if self.bus and "enabled" in body:
                self.bus.publish("privacy.set_enabled", {"enabled": bool(body["enabled"])})
            try:
                import yaml as _yaml
                _cfg_path = _ASSISTANT_CONFIG_PATH
                with open(_cfg_path) as _f:
                    _cfg = _yaml.safe_load(_f) or {}
                if "privacy" not in _cfg:
                    _cfg["privacy"] = {}
                for key in ("enabled", "rate_hz", "threshold", "look_away_angle_deg",
                            "cooldown_s", "clear_frames", "announce", "announce_text", "resume_text"):
                    if key in body:
                        _cfg["privacy"][key] = body[key]
                with open(_cfg_path, "w") as _f:
                    _yaml.dump(_cfg, _f, default_flow_style=False, allow_unicode=True)
            except Exception as _exc:
                log.warning("privacy settings: could not persist to YAML: %s", _exc)
            return {"ok": True}

        # ── Depth query ─────────────────────────────────────────────────

        @app.get("/api/depth/map")
        async def api_depth_map():
            import cv2 as _cv2
            import numpy as _np
            import io
            from fastapi.responses import Response as _Response
            payload = None
            if self._dense_stereo_svc is not None:
                payload = self._dense_stereo_svc.latest_payload()
            if payload is None:
                # Try bus last-value cache
                payload = self.bus.last("vision.depth_map") if self.bus else None
            if payload is None:
                raise HTTPException(503, "No depth map available — enable dense_depth in config")
            # Build colorized JPEG
            depth_list = payload.get("depth_m", [])
            if not depth_list:
                raise HTTPException(503, "Depth map empty")
            arr = _np.array(
                [[0.0 if v is None else float(v) for v in row] for row in depth_list],
                dtype=_np.float32,
            )
            min_d = float(payload.get("nearest_m") or 0.25)
            max_d = float(payload.get("farthest_m") or 6.0)
            if max_d <= min_d:
                max_d = min_d + 1.0
            normed = _np.clip((arr - min_d) / (max_d - min_d), 0.0, 1.0)
            normed = (normed * 255).astype(_np.uint8)
            colored = _cv2.applyColorMap(normed, _cv2.COLORMAP_TURBO)
            _, jpg_buf = _cv2.imencode(".jpg", colored, [_cv2.IMWRITE_JPEG_QUALITY, 85])
            return _Response(content=jpg_buf.tobytes(), media_type="image/jpeg")

        @app.get("/api/depth/query")
        async def api_depth_query():
            payload = None
            if self._dense_stereo_svc is not None:
                payload = self._dense_stereo_svc.latest_payload()
            if payload is None and self.bus:
                payload = self.bus.last("vision.depth_map")

            # Per-face depths from stereo face service
            face_depths: list = []
            if self.bus:
                fd = self.bus.last("vision.face_depth")
                if fd:
                    for f in fd.get("faces", []):
                        face_depths.append({
                            "face_id": f.get("face_id"),
                            "name": f.get("name"),
                            "depth_m": f.get("depth_m"),
                            "method": f.get("method"),
                        })

            if payload is None and not face_depths:
                return JSONResponse({"ok": False, "error": "No depth data available"}, status_code=503)

            result: dict = {"ok": True, "face_depths": face_depths}
            if payload:
                result.update({
                    "nearest_m": payload.get("nearest_m"),
                    "farthest_m": payload.get("farthest_m"),
                    "mean_m": payload.get("mean_m"),
                    "valid_pct": payload.get("valid_pct"),
                    "calibrated": payload.get("calibrated", False),
                    "method": payload.get("method", "unknown"),
                    "ts": payload.get("ts"),
                })
            else:
                result.update({
                    "nearest_m": min((f["depth_m"] for f in face_depths if f["depth_m"]), default=None),
                    "farthest_m": max((f["depth_m"] for f in face_depths if f["depth_m"]), default=None),
                    "mean_m": None,
                    "calibrated": False,
                    "method": "face_size",
                    "ts": None,
                })
            return JSONResponse(result)

        @app.get("/api/depth/mono/stats")
        async def api_depth_mono_stats():
            """Debug endpoint — returns raw payload stats without rendering."""
            import numpy as _np
            payload = None
            if self._mono_depth_svc is not None:
                payload = self._mono_depth_svc.latest_payload()
            if payload is None and self.bus:
                payload = self.bus.last("vision.mono_depth_map")
            if payload is None:
                return JSONResponse({"ok": False, "error": "no payload"})
            depth_list = payload.get("depth_rel", [])
            if depth_list:
                arr = _np.array([[float(v) if v is not None else float("nan") for v in row]
                                  for row in depth_list], dtype=_np.float32)
                return JSONResponse({
                    "ok": True,
                    "shape": list(arr.shape),
                    "dtype": str(arr.dtype),
                    "min": float(_np.nanmin(arr)),
                    "max": float(_np.nanmax(arr)),
                    "mean": float(_np.nanmean(arr)),
                    "nan_count": int(_np.isnan(arr).sum()),
                    "hardware_ready": payload.get("hardware_ready"),
                    "nearest_rel": payload.get("nearest_rel"),
                    "farthest_rel": payload.get("farthest_rel"),
                })
            return JSONResponse({"ok": False, "error": "depth_rel empty", "keys": list(payload.keys())})

        @app.get("/api/depth/mono")
        async def api_depth_mono():
            import cv2 as _cv2
            import numpy as _np
            from fastapi.responses import Response as _Response
            payload = None
            if self._mono_depth_svc is not None:
                payload = self._mono_depth_svc.latest_payload()
            if payload is None and self.bus:
                payload = self.bus.last("vision.mono_depth_map")
            if payload is None:
                raise HTTPException(503, "No mono depth map available — enable mono_depth in config")
            depth_list = payload.get("depth_rel", [])
            if not depth_list:
                raise HTTPException(503, "Mono depth map empty")
            arr = _np.array([[float(v) for v in row] for row in depth_list], dtype=_np.float32)
            arr = _np.nan_to_num(arr, nan=0.0, posinf=1.0, neginf=0.0)
            arr = _np.clip(arr, 0.0, 1.0)
            normed = (arr * 255).astype(_np.uint8)
            colored = _cv2.applyColorMap(normed, _cv2.COLORMAP_TURBO)
            _, jpg_buf = _cv2.imencode(".jpg", colored, [_cv2.IMWRITE_JPEG_QUALITY, 85])
            return _Response(content=jpg_buf.tobytes(), media_type="image/jpeg")


        @app.get("/api/music/status")
        async def api_music_status():
            if not self._music_svc:
                return {
                    "state": "stopped", "song": {}, "stations": [],
                    "configured": False, "elapsed_sec": 0, "duration_sec": 0,
                    "volume": -1, "eq_preset": "flat",
                }
            song = self._music_svc.current_song
            return {
                "state":        self._music_svc.state,
                "song":         song,
                "stations":     self._music_svc.stations,
                "configured":   self._music_svc.is_configured,
                "elapsed_sec":  song.get("elapsed_sec", 0),
                "duration_sec": song.get("duration_sec", 0),
                "volume":       self._music_svc.volume,
                "eq_preset":    self._music_svc.eq_preset,
            }

        @app.get("/api/music/volume")
        async def api_music_volume_get():
            if not self._music_svc:
                return {"level": -1}
            return {"level": self._music_svc.volume}

        @app.put("/api/music/volume")
        async def api_music_volume_set(body: _MusicVolumeBody):
            if self._music_svc:
                self._music_svc.set_volume(body.level)
            return {"ok": True, "level": body.level}

        @app.get("/api/music/eq")
        async def api_music_eq_get():
            preset = self._music_svc.eq_preset if self._music_svc else "flat"
            from src.services.music_service import MusicService as _MS
            return {"preset": preset, "presets": _MS.EQ_PRESETS}

        @app.put("/api/music/eq")
        async def api_music_eq_set(body: _MusicEqBody):
            if self._music_svc:
                self._music_svc.set_eq_preset(body.preset)
            return {"ok": True, "preset": body.preset}

        @app.get("/api/music/eq/custom")
        async def api_custom_eq_get():
            import json as _json
            from pathlib import Path as _Path
            state_file = _Path.home() / ".config" / "desktop-assistant" / "custom_eq.json"
            bands = []
            if state_file.exists():
                try:
                    bands = _json.loads(state_file.read_text())
                except Exception:
                    pass
            return {"bands": bands}

        @app.put("/api/music/eq/custom")
        async def api_custom_eq_set(body: _CustomEqBody):
            bands = [{"hz": b.hz, "gain_db": b.gain_db, "q": b.q} for b in body.bands]
            if self.bus:
                self.bus.publish("av.set_custom_eq", {"bands": bands})
                # Also update music_svc eq_preset tracker so UI stays consistent
                if self._music_svc:
                    self._music_svc._eq_preset = "custom"
                    # Persist so the "custom" selection survives daemon restarts.
                    try:
                        _p = Path.home() / ".config" / "desktop-assistant" / "music_eq_preset.txt"
                        _p.parent.mkdir(parents=True, exist_ok=True)
                        _p.write_text("custom")
                    except Exception:
                        pass
            return {"ok": True, "bands": bands}

        class _MusicPlayBody(BaseModel):
            station_id: Optional[int] = None

        class _MusicStationBody(BaseModel):
            station_id: int

        @app.post("/api/music/play")
        async def api_music_play(body: _MusicPlayBody = _MusicPlayBody()):
            payload = {}
            if body.station_id is not None:
                payload["station_id"] = body.station_id
            if self.bus:
                self.bus.publish("music.play", payload)
            return {"ok": True}

        @app.post("/api/music/stop")
        async def api_music_stop():
            if self.bus:
                self.bus.publish("music.stop", {})
            return {"ok": True}

        @app.post("/api/music/next")
        async def api_music_next():
            if self.bus:
                self.bus.publish("music.next", {})
            return {"ok": True}

        @app.post("/api/music/pause")
        async def api_music_pause():
            if self.bus:
                self.bus.publish("music.pause", {})
            return {"ok": True}

        @app.post("/api/music/thumbs-up")
        async def api_music_thumbs_up():
            if self.bus:
                self.bus.publish("music.thumbs_up", {})
            return {"ok": True}

        @app.post("/api/music/thumbs-down")
        async def api_music_thumbs_down():
            if self.bus:
                self.bus.publish("music.thumbs_down", {})
            return {"ok": True}

        @app.post("/api/music/station")
        async def api_music_station(body: _MusicStationBody):
            if self.bus:
                self.bus.publish("music.set_station", {"station_id": body.station_id})
            return {"ok": True}

        return app
