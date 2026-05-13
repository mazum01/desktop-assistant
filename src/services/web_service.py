"""
Web dashboard service — FastAPI app running inside the assistant process.

Serves a live dark-themed single-page dashboard on ``http://<pi>:8080``.

Endpoints
---------
GET  /                    Main dashboard HTML
GET  /stream              MJPEG camera stream (from bus vision.jpeg_ready frames)
WS   /ws                  Live JSON status + event tail (pushes every ~1 s)
GET  /api/status          One-shot status snapshot
GET  /api/faces           List all known faces
GET  /api/faces/{id}/thumb  Face thumbnail JPEG (64×64)
GET  /api/faces/{id}/photo  Full-size face photo JPEG (falls back to thumb)
PUT  /api/faces/{id}      Rename a face  body: {"name": "Alice"}
DEL  /api/faces           Delete ALL faces
DEL  /api/faces/guests    Delete only Guest-named faces
POST /api/faces/merge     Merge two faces  body: {"keep_id": "...", "absorb_id": "..."}
DEL  /api/faces/{id}      Delete a face and all its embeddings
POST /api/say             Speak text   body: {"text": "hello"}
POST /api/pan             Pan servo    body: {"angle": 180.0}
POST /api/version         Speak version number
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
GET  /api/settings/greeting  Get greeting config
PUT  /api/settings/greeting  Update greeting cooldown  body: {"cooldown_min": float}
POST /api/vision/describe    Speak natural-language description of current scene
GET  /api/settings/camera/rotation   Get camera 1 rotation angle
PUT  /api/settings/camera/rotation   Set camera 1 rotation  body: {"rotation_deg": int 0-359}
GET  /api/settings/camera2/rotation  Get camera 2 rotation angle
PUT  /api/settings/camera2/rotation  Set camera 2 rotation  body: {"rotation_deg": int 0-359}
GET  /api/settings/camera/resolution  Get current capture resolution (both cameras)
PUT  /api/settings/camera/resolution  Set capture resolution  body: {"width": int, "height": int}
GET  /api/music/eq/custom  Get current custom EQ bands
PUT  /api/music/eq/custom  Set custom EQ bands  body: {"bands": [...]}
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

from src.core.quiet_hours import QuietHours

from pydantic import BaseModel

log = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent.parent / "web" / "static"


class _RenameBody(BaseModel):
    name: str


class _QuietHoursBody(BaseModel):
    enabled: bool
    start: str
    end: str


class _SayBody(BaseModel):
    text: str


class _PanBody(BaseModel):
    angle: float


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
    ) -> None:
        self.bus = bus
        self._host = host
        self._port = port
        self._registry = registry
        self._vision_svc = vision_service
        self._quiet_hours = quiet_hours
        self._motion_svc = motion_service
        self._tracking_svc = tracking_service
        self._music_svc = music_service
        self._camera2_svc = camera2_service
        self._object_svc = object_service
        self._skills_svc = skills_service
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
        }

    # ── FastAPI app ───────────────────────────────────────────────────

    def _build_app(self):
        from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
        from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
        from fastapi.staticfiles import StaticFiles

        app = FastAPI(title="Desktop Assistant Dashboard", docs_url=None, redoc_url=None)

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

        @app.get("/api/settings/object-detection")
        async def api_get_object_detection():
            enabled = self._object_svc.detection_enabled if self._object_svc else True
            return {"enabled": enabled}

        @app.put("/api/settings/object-detection")
        async def api_put_object_detection(body: _ServoBody):
            if self.bus:
                self.bus.publish("object.set_enabled", {"enabled": body.enabled})
            return {"ok": True, "enabled": body.enabled}

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
            self.bus.publish("motion.pan_to", {"angle": body.angle})
            return {"ok": True}

        @app.post("/api/version")
        async def api_version():
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.announce_version", None)
            return {"ok": True}

        @app.post("/api/restart")
        async def api_restart():
            import asyncio
            import subprocess

            async def _do_restart():
                await asyncio.sleep(0.4)
                subprocess.Popen(
                    ["sudo", "systemctl", "restart", "desktop-assistant-core.service"],
                    close_fds=True,
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
            self.bus.publish("vision.describe", {})
            return {"ok": True}

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

        # ── Music (Pandora/pianobar) ────────────────────────────────────

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
