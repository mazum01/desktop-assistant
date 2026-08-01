"""Web entry point — the FastAPI dashboard/API (`WebService`), isolated from
`desktop-assistant-core`.

This is Phase 3 of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md: the
highest-effort, highest-value split, deliberately saved for last. Unlike
Phase 1 (`media`) and Phase 2a/2b (`integrations`), the direction of the
proxy relationship is reversed here — `WebService` is the piece that moves
*out* of core, and it's `WebService` that still needs synchronous reads
(and a couple of actions) from services that stay in `core`
(`RoomService`, `FaceService`, `PrivacyService`, `ObjectService`,
`PerceptionService`, `MotionService`, `TrackingService`, `VisionService`,
`RawCameraService`/camera2, `DenseStereoService`/`MonoDepthService`'s
enabled-flags) — see `src/core/web_client.py` for the proxies and the full
rationale for why most of `WebService`'s remaining coupling turned out to
be read-mostly (write/toggle routes already went through
`self.bus.publish(...)`, which crosses the process boundary for free).

`MusicService`/`PodcastService` (media) and `IoTService`/`SkillsService`
(integrations) proxies are unchanged from Phase 1/2b — this process builds
its own `IPCClient`s pointed at those processes' REP endpoints, exactly the
way `core_main.py` used to, since `WebService` (not `core_main.py`) is now
the thing that needs them.

Wiring
------
- Owns its own `MessageBus` + `IPCBridge`, exactly like `media_main.py`/
  `integrations_main.py`.
- Subscribes *upstream* to **every** other process's PUB endpoint (core,
  thermal, media, integrations) — not just core's. `WebService` used to
  share core's in-process bus and therefore saw every event core itself
  saw (including ones core only received by subscribing to thermal/media/
  integrations directly); IPCBridge forwarding is not transitive (core does
  not re-broadcast events it merely relayed from an upstream), so this
  process needs the same direct subscriptions core has, not just core's.
- `core_main.py`'s own `IPCBridge` adds this process's PUB endpoint as one
  more upstream (`_WEB_PUB`), so bus.publish() calls `WebService` makes
  that target core-resident services (`motion.pan_to`, `camera.set_rotation`,
  `tracking.set_*`, `privacy.set_*`, `anthropic.set_enabled`,
  `depth.set_*_enabled`, `room.set`, `face.*`, `object.set_enabled`,
  `av.*`, `voice.set_config`, `settings.quiet_hours_updated`) still reach
  them.
- `media_main.py` and `integrations_main.py` each also add `_WEB_PUB` to
  their own upstream_endpoints (see the wiring note in each), since
  `music.*` commands and `av.utterance` (SkillsService dispatch) now
  originate on *this* process's bus instead of core's.
- Registers no RPC handlers of its own — `WebService` is purely a
  consumer of other processes' RPCs in this topology (core, media,
  integrations), not a provider.

Run:
    python3 -m src.assistant.web_main

Or via systemd: services/systemd/desktop-assistant-web.service
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from src.core.integrations_client import IoTRegistryProxy, SkillsServiceProxy
from src.core.ipc_client import IPCClient
from src.core.media_client import MusicServiceProxy, PodcastServiceProxy
from src.core.process_node import ProcessNode
from src.core.quiet_hours import QuietHours
from src.core.web_client import build_web_proxies
from src.services.web_service import WebService

# Every other process's IPCBridge PUBs here; this process SUBs to all of
# them (not just core's) — see module docstring for why.
_CORE_PUB = "ipc:///tmp/desktop-assistant.pub"
_THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"
_MEDIA_PUB = "ipc:///tmp/desktop-assistant-media.pub"
_INTEGRATIONS_PUB = "ipc:///tmp/desktop-assistant-integrations.pub"

# REP endpoints this process calls *into* — core's own default IPCBridge
# REP (registered via `ipc.register_rpc(...)` in core_main.py), plus
# media's/integrations' (unchanged from Phase 1/2b).
_CORE_REP = "ipc:///tmp/desktop-assistant.rep"
_MEDIA_REP = "ipc:///tmp/desktop-assistant-media.rep"
_INTEGRATIONS_REP = "ipc:///tmp/desktop-assistant-integrations.rep"

# This process's own endpoints — core's IPCBridge adds WEB_PUB as one of
# its upstream_endpoints, symmetric to how it already does for thermal,
# media, and integrations.
WEB_PUB = "ipc:///tmp/desktop-assistant-web.pub"
WEB_REP = "ipc:///tmp/desktop-assistant-web.rep"

_CONFIG_DIR = Path(__file__).parents[2] / "config"


def _load_config() -> dict:
    import yaml

    cfg_path = _CONFIG_DIR / "assistant.yaml"
    return yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}


def build_node(
    cfg: dict,
    pub_endpoint: str = WEB_PUB,
    rep_endpoint: str = WEB_REP,
    upstream_endpoints: Optional[list] = None,
    core_rep: str = _CORE_REP,
    media_rep: str = _MEDIA_REP,
    integrations_rep: str = _INTEGRATIONS_REP,
) -> ProcessNode:
    """Build (but don't run) a fully-wired web `ProcessNode`.

    Factored out of `main()` so integration tests can construct a real node
    bound to unique test-only `ipc://` endpoints instead of the production
    ones, without duplicating the wiring logic (mirrors `media_main.py`/
    `integrations_main.py`'s `build_node()`).
    """
    node = ProcessNode(
        name="web",
        pub_endpoint=pub_endpoint,
        rep_endpoint=rep_endpoint,
        upstream_endpoints=(
            [_CORE_PUB, _THERMAL_PUB, _MEDIA_PUB, _INTEGRATIONS_PUB]
            if upstream_endpoints is None else upstream_endpoints
        ),
    )

    web_cfg = cfg.get("web_dashboard", {})
    web_enabled = bool(web_cfg.get("enabled", True))
    web_port = int(web_cfg.get("port", 8080))
    web_host = str(web_cfg.get("host", "0.0.0.0"))
    api_key = str(cfg.get("api_key", ""))

    qh = QuietHours.from_config(cfg_dir=_CONFIG_DIR, yaml_defaults=cfg.get("quiet_hours", {}))

    def _on_quiet_hours_updated(_topic, payload):
        if not isinstance(payload, dict):
            return
        try:
            qh.update(
                bool(payload.get("enabled", qh.enabled)),
                str(payload.get("start", qh.start)),
                str(payload.get("end", qh.end)),
            )
        except ValueError:
            pass

    node.bus.subscribe("settings.quiet_hours_updated", _on_quiet_hours_updated)

    core_client = IPCClient(core_rep)
    media_client = IPCClient(media_rep)
    integrations_client = IPCClient(integrations_rep)

    core_proxies = build_web_proxies(core_rep)
    music_proxy = MusicServiceProxy(media_client)
    podcast_proxy = PodcastServiceProxy(media_client)
    iot_registry_proxy = IoTRegistryProxy(integrations_client)
    skills_proxy = SkillsServiceProxy(integrations_client)

    web_svc: Optional[WebService] = None
    if web_enabled:
        web_svc = WebService(
            bus=node.bus,
            host=web_host,
            port=web_port,
            api_key=api_key,
            quiet_hours=qh,
            vision_service=core_proxies["vision"],
            motion_service=core_proxies["motion"],
            tracking_service=core_proxies["tracking"],
            camera2_service=core_proxies["camera2"],
            object_service=core_proxies["object"],
            perception_service=core_proxies["perception"],
            room_service=core_proxies["room"],
            face_service=core_proxies["face"],
            privacy_service=core_proxies["privacy"],
            depth_service=core_proxies["depth"],
            music_service=music_proxy,
            podcast_service=podcast_proxy,
            skills_service=skills_proxy,
            iot_registry=iot_registry_proxy,
        )
        node.add_service(web_svc)

    # Unused today but kept alive so callers depending on `node` for the
    # full wiring picture (tests) can introspect it; also keeps the
    # underlying REQ sockets from being garbage-collected mid-request.
    node._core_client = core_client  # noqa: SLF001

    if web_svc is not None:
        web_svc._all_services = node.services  # seed service registry at startup

    return node


def main() -> int:
    node = build_node(_load_config())
    return node.run()


if __name__ == "__main__":
    sys.exit(main())
