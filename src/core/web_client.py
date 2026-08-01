"""Web client proxies — drop-in stand-ins for the handful of `core`-resident
services that `WebService` still calls directly, for Phase 3 of
docs/architecture/PROCESS_ISOLATION_PROPOSAL.md (extracting `WebService`
itself into its own `web` process).

Unlike Phase 1/2b (where satellite services moved *out* of core and core grew
client proxies pointed *at* them), Phase 3 moves `WebService` itself out —
so the direction reverses: `web_main.py` builds these proxies pointed at
*core*'s own REP endpoint, and `core_main.py` registers the RPC handlers
against the real service objects that stay put.

Most of `WebService`'s remaining direct references turned out to be
read-mostly (a GET route reads a property or a small "status" method; the
paired PUT/action route already only does `self.bus.publish(...)`, which
crosses the process boundary for free via the existing IPCBridge upstream
mechanism — no proxy needed for those). Two services (`DenseStereoService`,
`MonoDepthService`) needed no proxy at all: every call site already had a
`self.bus.last(...)` fallback for when the direct reference is `None`, and
since both services publish their full payload on the bus on every update,
the fallback is always in sync — so the direct-object short-circuit was
simply removed instead of proxied (see `web_service.py`'s depth routes).
`FaceRegistry` (`self._registry`) also needed no proxy: `PerceptionService`
and `FaceService` already each open their own independent `FaceRegistry`
(sqlite) connection in the same process today, so `WebService` opening a
third connection from a different OS process is the same pattern it already
relies on, not a new risk.

Reply contract for every proxy call (mirrors `IPCClient.call()`'s contract,
same as `src/core/integrations_client.py`):
    {"ok": False, "error": "..."}                     — transport failure
                                                          (timeout, IPC down)
    {"ok": True, ...fields}                            — success
`WebService` route handlers check `ok` first (503 on transport failure),
then use the remaining fields directly, applying the same defaults the
in-process code used when the underlying service was `None`.
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from src.core.ipc_client import IPCClient

log = logging.getLogger(__name__)


class RoomServiceProxy:
    """Proxies `WebService`'s `/api/room*` and Anthropic-toggle needs."""

    name = "room"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "room.get_status"})
        if not reply.get("ok"):
            log.warning("core RPC failed (room.get_status): %s", reply.get("error"))
            return {}
        return reply.get("status", {})


class FaceServiceProxy:
    """Proxies `WebService`'s Anthropic-toggle fallback (used only when
    `RoomServiceProxy` is unavailable) and greeting-settings reads."""

    name = "face"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_anthropic_enabled(self, default: bool = True) -> bool:
        reply = self._client.call({"cmd": "face.get_anthropic_enabled"})
        if not reply.get("ok"):
            return default
        return bool(reply.get("enabled", default))

    def get_greeting_settings(self) -> dict:
        reply = self._client.call({"cmd": "face.get_greeting_settings"})
        if not reply.get("ok"):
            log.warning("core RPC failed (face.get_greeting_settings): %s", reply.get("error"))
            return {}
        return {
            "cooldown_min":         reply.get("cooldown_min", 30.0),
            "jitter_pct":           reply.get("jitter_pct", 25.0),
            "min_absence_s":        reply.get("min_absence_s", 30.0),
            "confidence_threshold": reply.get("confidence_threshold", 0.5),
        }


class PrivacyServiceProxy:
    """Proxies `WebService`'s `/api/settings/privacy` GET route."""

    name = "privacy"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "privacy.get_status"})
        if not reply.get("ok"):
            log.warning("core RPC failed (privacy.get_status): %s", reply.get("error"))
            return {}
        return reply.get("status", {})


class ObjectDetectionServiceProxy:
    """Proxies `WebService`'s `/api/settings/object-detection` GET route."""

    name = "object"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_enabled(self, default: bool = True) -> bool:
        reply = self._client.call({"cmd": "object.get_enabled"})
        if not reply.get("ok"):
            return default
        return bool(reply.get("enabled", default))


class PerceptionServiceProxy:
    """Proxies `WebService`'s `POST /api/faces/{id}/train` action."""

    name = "perception"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def capture_training_image(self, face_id: str) -> dict:
        reply = self._client.call({"cmd": "perception.capture_training_image", "face_id": face_id})
        if not reply.get("ok"):
            return {"ok": False, "reason": reply.get("error", "unavailable")}
        return reply.get("result", {"ok": False, "reason": "unknown"})


class MotionServiceProxy:
    """Proxies `WebService`'s `/api/settings/servo*` GET routes."""

    name = "motion"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "motion.get_status"})
        if not reply.get("ok"):
            log.warning("core RPC failed (motion.get_status): %s", reply.get("error"))
            return {}
        return reply.get("status", {})


class TrackingServiceProxy:
    """Proxies `WebService`'s `/api/settings/{face-tracking,random-motion,
    person-seek}` GET routes and `/api/tracking/params` GET/POST."""

    name = "tracking"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "tracking.get_status"})
        if not reply.get("ok"):
            log.warning("core RPC failed (tracking.get_status): %s", reply.get("error"))
            return {}
        return reply.get("status", {})

    def get_tunable_params(self) -> dict:
        reply = self._client.call({"cmd": "tracking.get_tunable_params"})
        if not reply.get("ok"):
            return {"params": {}, "ranges": {}, "presets": []}
        return reply.get("data", {"params": {}, "ranges": {}, "presets": []})

    def set_tunable_param(self, name: str, value: float) -> bool:
        reply = self._client.call({"cmd": "tracking.set_tunable_param", "name": name, "value": value})
        return bool(reply.get("ok") and reply.get("applied"))


class VisionServiceProxy:
    """Proxies `WebService`'s camera-1 (`VisionService`) needs: rotation/
    resolution settings, the MJPEG frame stream, and full-res snapshots."""

    name = "vision"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "vision.get_status"})
        if not reply.get("ok"):
            log.warning("core RPC failed (vision.get_status): %s", reply.get("error"))
            return {}
        return reply.get("status", {})

    def latest_jpeg(self) -> Optional[bytes]:
        reply = self._client.call({"cmd": "vision.latest_jpeg"}, timeout_ms=500)
        if not reply.get("ok"):
            return None
        data = reply.get("jpeg_b64")
        return base64.b64decode(data) if data else None

    def snapshot_jpeg(self, quality: int = 95) -> Optional[bytes]:
        reply = self._client.call({"cmd": "vision.snapshot_jpeg", "quality": quality}, timeout_ms=3000)
        if not reply.get("ok"):
            return None
        data = reply.get("jpeg_b64")
        return base64.b64decode(data) if data else None


class Camera2ServiceProxy:
    """Proxies `WebService`'s camera-2 (`RawCameraService`) needs — mirrors
    `VisionServiceProxy` but for the optional second camera.

    Camera 2 is either configured or not for the lifetime of the `core`
    process (never toggled at runtime), so `is_configured()` caches its
    result after the first successful RPC round trip instead of hitting the
    wire on every truthiness check `web_service.py` makes (it replaces what
    used to be a plain `self._camera2_svc is not None` check)."""

    name = "camera2"

    def __init__(self, client: IPCClient) -> None:
        self._client = client
        self._configured: Optional[bool] = None

    def is_configured(self) -> bool:
        if self._configured is not None:
            return self._configured
        reply = self._client.call({"cmd": "camera2.get_status"}, timeout_ms=500)
        if not reply.get("ok"):
            return False  # transport failure — don't cache, retry next time
        self._configured = bool(reply.get("configured"))
        return self._configured

    def get_status(self) -> dict:
        reply = self._client.call({"cmd": "camera2.get_status"})
        if not reply.get("ok"):
            return {}
        return reply.get("status", {})

    def latest_jpeg(self) -> Optional[bytes]:
        reply = self._client.call({"cmd": "camera2.latest_jpeg"}, timeout_ms=500)
        if not reply.get("ok"):
            return None
        data = reply.get("jpeg_b64")
        return base64.b64decode(data) if data else None

    def snapshot_jpeg(self, quality: int = 95) -> Optional[bytes]:
        reply = self._client.call({"cmd": "camera2.snapshot_jpeg", "quality": quality}, timeout_ms=3000)
        if not reply.get("ok"):
            return None
        data = reply.get("jpeg_b64")
        return base64.b64decode(data) if data else None


class DepthServicesProxy:
    """Proxies the two `_enabled` flags `WebService`'s `/api/settings/depth`
    GET route needs (`DenseStereoService`/`MonoDepthService`). Everything
    else in that route already comes from `bus.last(...)`."""

    name = "depth"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def get_enabled_flags(self) -> dict:
        reply = self._client.call({"cmd": "depth.get_enabled_flags"})
        if not reply.get("ok"):
            return {"dense_enabled": False, "mono_enabled": False}
        return reply.get("flags", {"dense_enabled": False, "mono_enabled": False})


def build_web_proxies(rep_endpoint: str, timeout_ms: int = 2000) -> dict:
    """Convenience factory: one `IPCClient` shared by every proxy `web_main.py`
    needs, keyed by name for easy unpacking at the `WebService(...)` call
    site."""
    client = IPCClient(rep_endpoint, timeout_ms=timeout_ms)
    return {
        "room": RoomServiceProxy(client),
        "face": FaceServiceProxy(client),
        "privacy": PrivacyServiceProxy(client),
        "object": ObjectDetectionServiceProxy(client),
        "perception": PerceptionServiceProxy(client),
        "motion": MotionServiceProxy(client),
        "tracking": TrackingServiceProxy(client),
        "vision": VisionServiceProxy(client),
        "camera2": Camera2ServiceProxy(client),
        "depth": DepthServicesProxy(client),
    }
