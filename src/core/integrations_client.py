"""Integrations client proxies — drop-in stand-ins for `IoTService`'s
registry and `SkillsService` that live in `desktop-assistant-core`'s process
after those two services were extracted into the `integrations` process
(see docs/architecture/PROCESS_ISOLATION_PROPOSAL.md, Phase 2b).

Unlike Phase 1 (`media_client.py`), `WebService`'s coupling to the IoT
registry and skills service isn't a handful of read-only properties — it's
multi-step "get an object, then call methods on it" code (`registry.get(id)`
then `dev.execute_action(...)`), and `api_iot_add` used to construct the
device object itself using `bus=self.bus`. None of that can cross a process
boundary by duck-typing alone, so instead of mirroring the original objects'
shape 1:1, each proxy exposes **one method per `WebService` HTTP route**,
and the corresponding RPC handler in `integrations_main.py` does the actual
object manipulation locally (where the registry/registry devices/skills
actually live).

Reply contract for every proxy call (mirrors `IPCClient.call()`'s own
contract so callers can chain the checks):
    {"ok": False, "error": "..."}                     — transport failure
                                                          (timeout, IPC down)
    {"ok": False, "reason": "not_found"}               — resource not found
    {"ok": False, "reason": "bad_request", "error":..} — bad input / device
                                                          op raised
    {"ok": True, ...fields}                            — success
`WebService` route handlers check `ok` first (503 on transport failure),
then `reason` (404 for not_found, 400 for bad_request), then use the
remaining fields directly — same status codes as before the split.
"""

from __future__ import annotations

import logging

from src.core.ipc_client import IPCClient

log = logging.getLogger(__name__)


class IoTRegistryProxy:
    """Proxies `WebService`'s IoT-route needs to the `integrations` process."""

    name = "iot"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def _call(self, cmd: str, **payload) -> dict:
        return self._client.call({"cmd": cmd, **payload})

    def get_all_snapshots(self) -> dict:
        """Used by `/api/status`'s ``iot`` field."""
        reply = self._call("iot.snapshots")
        if not reply.get("ok"):
            log.warning("integrations RPC failed (iot.snapshots): %s", reply.get("error"))
            return {}
        return reply.get("snapshots", {})

    def list_all(self) -> dict:
        return self._call("iot.list")

    def add(self, type_id: str, config: dict) -> dict:
        return self._call("iot.add", type_id=type_id, config=config)

    def get_detail(self, device_id: str) -> dict:
        return self._call("iot.get_detail", device_id=device_id)

    def update_config(self, device_id: str, config_patch: dict) -> dict:
        return self._call("iot.update_config", device_id=device_id, config=config_patch)

    def delete(self, device_id: str) -> dict:
        return self._call("iot.delete", device_id=device_id)

    def announce(self, device_id: str) -> dict:
        return self._call("iot.announce", device_id=device_id)

    def execute_action(self, device_id: str, action: str, params: dict) -> dict:
        return self._call("iot.action", device_id=device_id, action=action, params=params)


class SkillsServiceProxy:
    """Proxies `WebService`'s skills-route needs to the `integrations` process."""

    name = "skills"

    def __init__(self, client: IPCClient) -> None:
        self._client = client

    def _call(self, cmd: str, **payload) -> dict:
        return self._client.call({"cmd": cmd, **payload})

    def list_skills(self) -> dict:
        reply = self._call("skills.list")
        if not reply.get("ok"):
            log.warning("integrations RPC failed (skills.list): %s", reply.get("error"))
            return {"skills": []}
        return reply

    def set_enabled(self, skill_name: str, enabled: bool) -> dict:
        return self._call("skills.set_enabled", skill_name=skill_name, enabled=enabled)

    def get_config(self, skill_name: str) -> dict:
        return self._call("skills.get_config", skill_name=skill_name)

    def set_config(self, skill_name: str, key: str, value) -> dict:
        return self._call("skills.set_config", skill_name=skill_name, key=key, value=value)


def build_integrations_proxies(rep_endpoint: str, timeout_ms: int = 2000) -> tuple:
    """Convenience factory: one `IPCClient` shared by both proxies."""
    client = IPCClient(rep_endpoint, timeout_ms=timeout_ms)
    return IoTRegistryProxy(client), SkillsServiceProxy(client)
