"""
Web dashboard service — FastAPI app running inside the assistant process.

Serves a live dark-themed single-page dashboard on ``http://<pi>:8080``.

Endpoints
---------
GET  /                    Main dashboard HTML
GET  /stream              MJPEG camera stream (from bus vision.frame_ready frames)
WS   /ws                  Live JSON status + event tail (pushes every ~1 s)
GET  /api/status          One-shot status snapshot
GET  /api/faces           List all known faces
GET  /api/faces/{id}/thumb  Face thumbnail JPEG
PUT  /api/faces/{id}      Rename a face  body: {"name": "Alice"}
DEL  /api/faces/{id}      Delete a face and all its embeddings
POST /api/say             Speak text   body: {"text": "hello"}
POST /api/pan             Pan servo    body: {"angle": 180.0}
POST /api/version         Speak version number
GET  /api/settings/servo  Get servo enabled state
PUT  /api/settings/servo  Set servo enabled state  body: {"enabled": bool}
"""

import asyncio
import io
import json
import logging
import threading
import time
from pathlib import Path
from typing import Optional

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


class _ServoBody(BaseModel):
    enabled: bool


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
    ) -> None:
        self.bus = bus
        self._host = host
        self._port = port
        self._registry = registry
        self._vision_svc = vision_service
        self._quiet_hours = quiet_hours
        self._motion_svc = motion_service
        self._server = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._latest_frame: Optional[bytes] = None   # JPEG bytes
        self._frame_event = threading.Event()
        self._ws_clients: list = []
        self._event_log: list[dict] = []             # recent bus events (capped)
        self._unsubs: list = []
        self._running = False

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

        # Subscribe to camera frames
        self._unsubs.append(
            self.bus.subscribe("vision.frame_ready", self._on_frame)
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

    _STREAM_WIDTH  = 640
    _STREAM_HEIGHT = 360

    def _on_frame(self, _topic, payload) -> None:
        """Grab the latest frame from VisionService, resize for stream, cache as JPEG."""
        if self._vision_svc is None:
            return
        try:
            frame = self._vision_svc.latest_frame()
            if frame is None:
                return
            import cv2
            small = cv2.resize(frame, (self._STREAM_WIDTH, self._STREAM_HEIGHT),
                               interpolation=cv2.INTER_LINEAR)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 65])
            if ok:
                self._latest_frame = bytes(buf)
        except Exception:
            pass

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
        return {
            "version": get_version(),
            "ts": time.time(),
            "last": last,
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
                while True:
                    frame = self._latest_frame
                    if frame:
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n"
                        )
                    await asyncio.sleep(0.05)  # ~20 fps cap

            return StreamingResponse(
                generate(),
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

        @app.delete("/api/faces")
        async def api_delete_all_faces():
            if not self._registry:
                raise HTTPException(503, "registry unavailable")
            count = self._registry.delete_all_faces()
            if self.bus:
                self.bus.publish("face.registry_cleared", {"count": count})
            return {"ok": True, "deleted": count}

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

        # ── REST: controls ────────────────────────────────────────────

        @app.post("/api/say")
        async def api_say(body: _SayBody):
            if not self.bus:
                raise HTTPException(503, "bus unavailable")
            self.bus.publish("av.say", {"text": body.text})
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

        return app
