"""
Integrations entry point — Telegram notifications, thermal/absence alerts,
and clock announcements, isolated from `desktop-assistant-core`.

This is Phase 2a of docs/architecture/PROCESS_ISOLATION_PROPOSAL.md's
`integrations` group. It covers the three services that have **zero**
`WebService` object coupling — `TelegramService`, `NotificationService`,
`ClockService` — the same "no proxy needed" shape that made `media`
(Phase 1) low-risk. `IoTService` and `SkillsService` (the other two members
of the `integrations` group) stay in `core` for now (Phase 2b): both are
held directly by `WebService` (`iot_registry`: 10 call sites, `skills_svc`:
4 call sites) and need `media_client.py`-style proxies before they can move.

Wiring
------
- Owns its own `MessageBus` + `IPCBridge`, exactly like `media_main.py`.
- Subscribes *upstream* to core's PUB endpoint so events these services
  react to but that originate in core (`face.greeted`, `av.spoke`,
  `av.speaking_started`, `perception.faces`, `av.tell_joke`,
  `av.announce_time`, `telegram.send`, `settings.quiet_hours_updated`)
  reach this process's local bus, where the normal `bus.subscribe(...)`
  handlers pick them up unchanged.
- Also subscribes directly to *thermal's* PUB endpoint for `thermal.temp` —
  core's own IPCBridge does NOT re-forward events it received from an
  upstream (that guard is what prevents forwarding loops), so relying on
  core as a relay for thermal data would silently starve
  `NotificationService`'s thermal alerts. Every process that needs
  thermal telemetry subscribes to `_THERMAL_PUB` directly, same as core
  does today.
- `core_main.py`'s own `IPCBridge` adds this process's PUB endpoint as one
  more upstream (alongside thermal's and media's), so `av.say` calls made
  here (clock announcements, thermal/absence alerts) still reach
  `AVService`/TTS output, which stays in core.
- Keeps its own `QuietHours` instance in sync with core's by subscribing
  to `settings.quiet_hours_updated` (published by `WebService` and
  `QuietHoursSkill`, both still in core) rather than sharing the object
  directly, since object references don't cross process boundaries.

Run:
    python3 -m src.assistant.integrations_main

Or via systemd: services/systemd/desktop-assistant-integrations.service
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from src.core.process_node import ProcessNode
from src.core.quiet_hours import QuietHours
from src.services.clock_service import ClockService
from src.services.notification_service import NotificationService
from src.services.telegram_service import TelegramService

# Core's IPCBridge PUBs here; we SUBscribe to it so events originating in
# core (face.greeted, av.spoke, perception.faces, av.tell_joke, ...) reach
# these services' bus.subscribe() handlers running in this process.
_CORE_PUB = "ipc:///tmp/desktop-assistant.pub"

# Thermal PUBs directly here too — see module docstring for why this can't
# be relied on to come transitively through core.
_THERMAL_PUB = "ipc:///tmp/desktop-assistant-thermal.pub"

# This process's own endpoints — core's IPCBridge adds INTEGRATIONS_PUB as
# one of its upstream_endpoints, symmetric to how it already does for
# thermal and media.
INTEGRATIONS_PUB = "ipc:///tmp/desktop-assistant-integrations.pub"
INTEGRATIONS_REP = "ipc:///tmp/desktop-assistant-integrations.rep"

# Module-level so tests can monkeypatch it to a tmp_path, keeping this
# process's QuietHours instance from touching the real on-disk config
# (mirrors the monkeypatch pattern test_media_process_split.py uses for
# MusicService/PodcastService's state file constants).
_CONFIG_DIR = Path(__file__).parents[2] / "config"


def _load_config() -> dict:
    import yaml

    cfg_path = _CONFIG_DIR / "assistant.yaml"
    return yaml.safe_load(cfg_path.read_text()) if cfg_path.exists() else {}


def build_node(
    cfg: dict,
    pub_endpoint: str = INTEGRATIONS_PUB,
    rep_endpoint: str = INTEGRATIONS_REP,
    upstream_endpoints: Optional[list] = None,
) -> ProcessNode:
    """Build (but don't run) a fully-wired integrations `ProcessNode`.

    Factored out of `main()` so integration tests can construct a real node
    bound to unique test-only `ipc://` endpoints instead of the production
    ones, without duplicating the wiring logic.
    """
    node = ProcessNode(
        name="integrations",
        pub_endpoint=pub_endpoint,
        rep_endpoint=rep_endpoint,
        upstream_endpoints=(
            [_CORE_PUB, _THERMAL_PUB] if upstream_endpoints is None else upstream_endpoints
        ),
    )

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

    clock_enabled = cfg.get("clock_announcements", {}).get("enabled", True)
    clock = ClockService(bus=node.bus, enabled=bool(clock_enabled), quiet_hours=qh)

    notif_cfg = cfg.get("notifications", {})
    notif_thermal_cfg = notif_cfg.get("thermal_alerts", {})
    notif_absence_cfg = notif_cfg.get("absence_alerts", {})
    notif = NotificationService(
        bus=node.bus,
        quiet_hours=qh,
        thermal_alerts_enabled=bool(notif_thermal_cfg.get("enabled", True)),
        warn_celsius=float(notif_thermal_cfg.get("warn_celsius", 75.0)),
        critical_celsius=float(notif_thermal_cfg.get("critical_celsius", 85.0)),
        thermal_rate_limit_min=float(notif_thermal_cfg.get("min_interval_min", 10.0)),
        absence_alerts_enabled=bool(notif_absence_cfg.get("enabled", True)),
        absence_min=float(notif_absence_cfg.get("absence_min", 30.0)),
        absence_rate_limit_min=float(notif_absence_cfg.get("min_interval_min", 60.0)),
    )

    tg_cfg = cfg.get("telegram", {})
    telegram = TelegramService(
        bus=node.bus,
        enabled=bool(tg_cfg.get("enabled", False)),
        bot_token=str(tg_cfg.get("bot_token", "")),
        chat_id=str(tg_cfg.get("chat_id", "")),
        emoji_map={
            "new_face":  tg_cfg.get("emoji_new_face", "👋"),
            "returning": tg_cfg.get("emoji_returning", "👤"),
            "named":     tg_cfg.get("emoji_named", "🏷️"),
        },
    )

    node.add_services(clock, notif, telegram)
    return node


def main() -> int:
    node = build_node(_load_config())
    return node.run()


if __name__ == "__main__":
    sys.exit(main())
