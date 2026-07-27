"""
Integrations entry point — Telegram notifications, thermal/absence alerts,
clock announcements, IoT devices, and voice-intent skills, isolated from
`desktop-assistant-core`.

This covers the full `integrations` group of
docs/architecture/PROCESS_ISOLATION_PROPOSAL.md:
- Phase 2a: `TelegramService`, `NotificationService`, `ClockService` — the
  three services with **zero** `WebService` object coupling, the same
  "no proxy needed" shape that made `media` (Phase 1) low-risk.
- Phase 2b: `IoTService`, `SkillsService` — both were held directly by
  `WebService` (`iot_registry`: ~10 call sites, `skills_svc`: 4 call sites)
  via multi-step "get an object, then call methods on it" code, which can't
  cross a process boundary by duck-typing alone. `src/core/integrations_client.py`
  provides `IoTRegistryProxy`/`SkillsServiceProxy`, and this module registers
  one RPC handler per `WebService` route that needs one (see that module's
  docstring for the full reply contract).

Wiring
------
- Owns its own `MessageBus` + `IPCBridge`, exactly like `media_main.py`.
- Subscribes *upstream* to core's PUB endpoint so events these services
  react to but that originate in core (`face.greeted`, `av.spoke`,
  `av.speaking_started`, `perception.faces`, `av.tell_joke`,
  `av.announce_time`, `telegram.send`, `settings.quiet_hours_updated`,
  `av.utterance` for `SkillsService`) reach this process's local bus,
  where the normal `bus.subscribe(...)` handlers pick them up unchanged.
- Also subscribes directly to *thermal's* PUB endpoint for `thermal.temp` —
  core's own IPCBridge does NOT re-forward events it received from an
  upstream (that guard is what prevents forwarding loops), so relying on
  core as a relay for thermal data would silently starve
  `NotificationService`'s thermal alerts and `SkillsService`'s
  `SystemStatusSkill` live data. Every process that needs thermal
  telemetry subscribes to `_THERMAL_PUB` directly, same as core does today.
- `core_main.py`'s own `IPCBridge` adds this process's PUB endpoint as one
  more upstream (alongside thermal's and media's), so `av.say` calls made
  here (clock announcements, thermal/absence alerts, skill responses) still
  reach `AVService`/TTS output, which stays in core.
- IMPORTANT knock-on effect of moving `SkillsService` here: `MusicControlSkill`/
  `VolumeSkill` publish `music.*` commands. Those now originate on *this*
  process's bus instead of core's, so `media_main.py` must also subscribe
  to this process's PUB (added alongside its existing core subscription) —
  otherwise music voice commands would silently stop working. See the
  `_INTEGRATIONS_PUB` addition in `media_main.py`.
- Keeps its own `QuietHours` instance in sync with core's by subscribing
  to `settings.quiet_hours_updated` (published by `WebService` and
  `QuietHoursSkill`, the latter now running here) rather than sharing the
  object directly, since object references don't cross process boundaries.

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
from src.services.iot_service import IoTService
from src.services.notification_service import NotificationService
from src.services.skills_service import SkillsService
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

    iot_svc = IoTService(bus=node.bus, cfg=cfg)

    skills_svc = SkillsService(bus=node.bus, quiet_hours=qh)

    node.add_services(clock, notif, telegram, iot_svc, skills_svc)

    # ── IoT RPCs ─────────────────────────────────────────────────────
    # See src/core/integrations_client.py for the reply contract
    # ("ok" = transport-level success, "reason" = not_found/bad_request).

    def _rpc_iot_snapshots(_msg):
        return {"ok": True, "snapshots": iot_svc.registry.get_all_snapshots()}

    def _rpc_iot_list(_msg):
        return {
            "ok": True,
            "devices": iot_svc.registry.get_device_list(),
            "snapshots": iot_svc.registry.get_all_snapshots(),
        }

    def _rpc_iot_add(msg):
        from src.iot import loader as iot_loader

        type_id = str(msg.get("type_id") or "").strip()
        cfg_in = msg.get("config") or {}
        if not type_id:
            return {"ok": False, "reason": "bad_request", "error": "type_id is required"}
        if not isinstance(cfg_in, dict):
            return {"ok": False, "reason": "bad_request", "error": "config must be an object"}
        try:
            dev = iot_loader.create_device(type_id, cfg_in, bus=node.bus)
            iot_svc.registry.register(dev)
            dev.start()
            iot_loader.save_persisted(iot_svc.registry)
        except Exception as exc:
            return {"ok": False, "reason": "bad_request", "error": str(exc)}
        return {
            "ok": True,
            "device_id": dev.device_id,
            "device_name": dev.device_name,
            "device_icon": dev.device_icon,
        }

    def _rpc_iot_get_detail(msg):
        device_id = msg.get("device_id", "")
        dev = iot_svc.registry.get(device_id)
        if dev is None:
            return {"ok": False, "reason": "not_found"}
        try:
            snap = dev.get_snapshot()
        except Exception as exc:
            return {"ok": False, "reason": "bad_request", "error": str(exc)}
        return {
            "ok": True,
            "device_id": dev.device_id,
            "device_name": dev.device_name,
            "device_icon": dev.device_icon,
            "config": dict(getattr(dev, "_cfg", {}) or {}),
            "actions": dev.get_actions(),
            "snapshot": snap,
        }

    def _rpc_iot_update_config(msg):
        from src.iot import loader as iot_loader

        device_id = msg.get("device_id", "")
        dev = iot_svc.registry.get(device_id)
        if dev is None:
            return {"ok": False, "reason": "not_found"}
        cfg_patch = msg.get("config")
        if not isinstance(cfg_patch, dict):
            return {"ok": False, "reason": "bad_request", "error": "config object is required"}
        merged = dict(getattr(dev, "_cfg", {}) or {})
        merged.update(cfg_patch)
        try:
            dev.stop()
            dev._cfg = merged
            dev.start()
            iot_loader.save_persisted(iot_svc.registry)
        except Exception as exc:
            return {"ok": False, "reason": "bad_request", "error": str(exc)}
        return {"ok": True, "device_id": device_id, "config": merged}

    def _rpc_iot_delete(msg):
        from src.iot import loader as iot_loader

        device_id = msg.get("device_id", "")
        dev = iot_svc.registry.get(device_id)
        if dev is None:
            return {"ok": False, "reason": "not_found"}
        iot_svc.registry.unregister(device_id)
        iot_loader.save_persisted(iot_svc.registry)
        return {"ok": True}

    def _rpc_iot_announce(msg):
        device_id = msg.get("device_id", "")
        dev = iot_svc.registry.get(device_id)
        if dev is None:
            return {"ok": False, "reason": "not_found"}
        text = dev.announce()
        return {"ok": True, "text": text}

    def _rpc_iot_action(msg):
        device_id = msg.get("device_id", "")
        dev = iot_svc.registry.get(device_id)
        if dev is None:
            return {"ok": False, "reason": "not_found"}
        action = str(msg.get("action") or "").strip()
        params = msg.get("params") or {}
        if not action:
            return {"ok": False, "reason": "bad_request", "error": "action is required"}
        if not isinstance(params, dict):
            return {"ok": False, "reason": "bad_request", "error": "params must be an object"}
        try:
            result = dev.execute_action(action, params=params)
        except Exception as exc:
            return {"ok": False, "reason": "bad_request", "error": str(exc)}
        payload = result if isinstance(result, dict) else {"ok": True, "result": result}
        return {"ok": True, "payload": payload}

    node.register_rpc("iot.snapshots", _rpc_iot_snapshots)
    node.register_rpc("iot.list", _rpc_iot_list)
    node.register_rpc("iot.add", _rpc_iot_add)
    node.register_rpc("iot.get_detail", _rpc_iot_get_detail)
    node.register_rpc("iot.update_config", _rpc_iot_update_config)
    node.register_rpc("iot.delete", _rpc_iot_delete)
    node.register_rpc("iot.announce", _rpc_iot_announce)
    node.register_rpc("iot.action", _rpc_iot_action)

    # ── Skills RPCs ──────────────────────────────────────────────────

    def _rpc_skills_list(_msg):
        skills_info = []
        for skill in skills_svc.registry.skills:
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
        return {"ok": True, "skills": skills_info}

    def _rpc_skills_set_enabled(msg):
        skill_name = msg.get("skill_name", "")
        skill = skills_svc.find_skill(skill_name)
        if skill is None:
            return {"ok": False, "reason": "not_found"}
        skill.enabled = bool(msg.get("enabled"))
        return {"ok": True, "name": skill_name, "enabled": skill.enabled}

    def _rpc_skills_get_config(msg):
        skill_name = msg.get("skill_name", "")
        skill = skills_svc.find_skill(skill_name)
        if skill is None:
            return {"ok": False, "reason": "not_found"}
        return {
            "ok": True,
            "name": skill_name,
            "schema": [f.as_dict() for f in skill.config_schema],
            "values": skill.get_config(),
        }

    def _rpc_skills_set_config(msg):
        skill_name = msg.get("skill_name", "")
        skill = skills_svc.find_skill(skill_name)
        if skill is None:
            return {"ok": False, "reason": "not_found"}
        key = msg.get("key")
        value = msg.get("value")
        try:
            skill.set_config(key, value)
        except ValueError as exc:
            return {"ok": False, "reason": "bad_request", "error": str(exc)}
        return {"ok": True, "name": skill_name, "key": key, "value": value}

    node.register_rpc("skills.list", _rpc_skills_list)
    node.register_rpc("skills.set_enabled", _rpc_skills_set_enabled)
    node.register_rpc("skills.get_config", _rpc_skills_get_config)
    node.register_rpc("skills.set_config", _rpc_skills_set_config)

    return node


def main() -> int:
    node = build_node(_load_config())
    return node.run()


if __name__ == "__main__":
    sys.exit(main())
