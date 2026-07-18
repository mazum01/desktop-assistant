"""
ProcessNode — reusable scaffolding for running a group of services as an
independent OS process that still participates in the whole-system message
bus.

This generalizes the pattern already proven in production by
`src/assistant/thermal_main.py` (the thermal-safety process) so it can be
reused to split additional service groups out of the monolithic
`desktop-assistant-core` process without inventing a new IPC mechanism.

Design
------
Each `ProcessNode`:
  * owns its own in-process `MessageBus` (services attached to this node
    talk to each other exactly as they do today — no code changes needed
    inside a `Service` subclass);
  * runs an `IPCBridge` that PUBs every local event over ZeroMQ and can
    SUB to one or more *upstream* nodes' PUB endpoints, forwarding their
    events onto the local bus (this is how the core process today sees
    `thermal.*` events without sharing memory with the thermal process);
  * can register RPC handlers via `IPCBridge.register_rpc()` so other
    processes can make synchronous request/reply calls into it (already
    the mechanism the CLI uses for `fan-control`, `servo`, etc.).

This module does NOT change how any existing service is wired today. It
is additive scaffolding intended to make the next extraction (e.g. moving
`music`+`podcast` or `web` into their own process) a matter of writing a
short `*_main.py` entry point instead of re-deriving the IPC wiring each
time.

See docs/architecture/PROCESS_ISOLATION_PROPOSAL.md for the full design
rationale and phased rollout plan this scaffolding supports.
"""

from __future__ import annotations

import logging
from typing import Callable, Iterable, List, Optional

from src.core.bus import MessageBus
from src.core.service import Service
from src.services.ipc_bridge import IPCBridge

log = logging.getLogger(__name__)


class ProcessNode:
    """A named, independently-runnable group of `Service` instances.

    Example (mirrors thermal_main.py)::

        node = ProcessNode(
            name="media",
            pub_endpoint="ipc:///tmp/desktop-assistant-media.pub",
            rep_endpoint="ipc:///tmp/desktop-assistant-media.rep",
            upstream_endpoints=["ipc:///tmp/desktop-assistant.pub"],
        )
        music = MusicService(bus=node.bus)
        podcast = PodcastService(bus=node.bus)
        node.add_services(music, podcast)
        raise SystemExit(node.run())
    """

    def __init__(
        self,
        name: str,
        pub_endpoint: str,
        rep_endpoint: str,
        upstream_endpoints: Optional[Iterable[str]] = None,
        bus: Optional[MessageBus] = None,
    ) -> None:
        self.name = name
        self.bus = bus or MessageBus()
        self.ipc = IPCBridge(
            bus=self.bus,
            pub_endpoint=pub_endpoint,
            rep_endpoint=rep_endpoint,
            upstream_endpoints=list(upstream_endpoints) if upstream_endpoints else None,
        )
        self._services: List[Service] = [self.ipc]

    def add_service(self, service: Service) -> "ProcessNode":
        self._services.append(service)
        return self

    def add_services(self, *services: Service) -> "ProcessNode":
        for svc in services:
            self.add_service(svc)
        return self

    def register_rpc(self, cmd: str, fn: Callable[[dict], dict]) -> "ProcessNode":
        """Expose *fn* to other processes as REP command *cmd*."""
        self.ipc.register_rpc(cmd, fn)
        return self

    @property
    def services(self) -> List[Service]:
        """All services registered on this node, including the IPCBridge."""
        return list(self._services)

    def run(self) -> int:
        """Start every registered service and block until signalled to stop.

        Delegates to the same `run_services()` boot/shutdown loop every
        other entry point (`core_main`, `thermal_main`) already uses, so
        this node gets identical signal handling, structured logging, and
        boot self-test behavior for free.
        """
        from src.assistant.runner import run_services

        # Seed IPCBridge's status view so `status`/`ping` RPCs work
        # immediately, matching what core_main.py does for its own bridge.
        self.ipc._all_services = self._services  # noqa: SLF001 (internal, same-package wiring)
        return run_services(self._services, unit_name=self.name)
