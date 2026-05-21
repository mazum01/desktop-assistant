"""Desktop Assistant — System Health Watchdog.

Monitors all critical services and auto-restarts any that fail or become
stuck.  Sends a Telegram notification whenever it intervenes.

Services monitored
------------------
* desktop-assistant-core    — systemctl + HTTP ping (localhost:8080/health)
* desktop-assistant-thermal — systemctl only (no HTTP interface)
* openclaw-gateway          — systemctl + HTTP ping (localhost:18789)
                              + journal scan for stuck "Bot not initialized" loop

Restart guard
-------------
Each service has an independent cooldown (default 5 min) so a repeatedly
crashing service doesn't trigger an infinite restart storm.

Telegram notifications
----------------------
Sent directly via Bot API (no dependency on the running assistant).
Uses the same bot_token / chat_id from config/assistant.yaml.

Run as
------
    python3 -m src.watchdog.watchdog              # foreground / dev
    systemctl start desktop-assistant-watchdog    # production

Config (config/assistant.yaml)
-------------------------------
watchdog:
  enabled: true
  check_interval_s: 30
  restart_cooldown_min: 5
  telegram_notify: true
  # Per-service overrides (optional):
  openclaw_stuck_threshold: 10   # "Bot not initialized" lines in 60s → restart
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("watchdog")

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL_S       = 30.0
_DEFAULT_COOLDOWN_MIN     = 5.0
_DEFAULT_STUCK_THRESHOLD  = 10   # "Bot not initialized" occurrences / 60 s

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / "config" / "assistant.yaml"

# ---------------------------------------------------------------------------
# Telegram helper (standalone — no bus dependency)
# ---------------------------------------------------------------------------

def _tg_send(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if not result.get("ok"):
                log.warning("Telegram API returned not-ok: %s", result)
    except Exception as exc:
        log.warning("Telegram notify failed: %s", exc)


# ---------------------------------------------------------------------------
# Service descriptor
# ---------------------------------------------------------------------------

@dataclass
class ManagedService:
    unit: str
    http_check: Optional[str] = None          # URL to GET; expects {"ok": true}
    http_timeout_s: float = 5.0
    journal_stuck_pattern: Optional[str] = None  # grep in last-60s journal
    stuck_threshold: int = _DEFAULT_STUCK_THRESHOLD
    # Set False for services that self-manage single-instance (e.g. openclaw exits
    # 78 when a healthy instance is already running, so systemd shows "inactive").
    # In that case only the HTTP check determines health.
    require_systemd_active: bool = True

    # Runtime state — not part of config
    last_restart_ts: float = field(default=0.0, init=False, repr=False)
    consecutive_failures: int = field(default=0, init=False, repr=False)

    def is_systemd_active(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", self.unit],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False

    def is_http_healthy(self) -> bool:
        if not self.http_check:
            return True
        try:
            with urllib.request.urlopen(self.http_check, timeout=self.http_timeout_s) as resp:
                data = json.loads(resp.read())
                return bool(data.get("ok") or data.get("status") == "live")
        except Exception:
            return False

    def is_journal_stuck(self) -> bool:
        """Return True if the stuck pattern appears ≥ stuck_threshold times in the last 60 s."""
        if not self.journal_stuck_pattern:
            return False
        try:
            result = subprocess.run(
                ["journalctl", "-u", self.unit, "--since", "60 seconds ago",
                 "--no-pager", "-q"],
                capture_output=True, text=True, timeout=8,
            )
            count = result.stdout.count(self.journal_stuck_pattern)
            if count >= self.stuck_threshold:
                log.warning("%s: stuck pattern %r seen %d times in last 60s",
                            self.unit, self.journal_stuck_pattern, count)
                return True
        except Exception:
            pass
        return False

    def is_healthy(self) -> tuple[bool, str]:
        """Return (healthy, reason). reason is '' when healthy."""
        if self.require_systemd_active and not self.is_systemd_active():
            return False, "systemd unit inactive/failed"
        if not self.is_http_healthy():
            return False, f"HTTP health check failed ({self.http_check})"
        if self.is_journal_stuck():
            return False, f"stuck loop detected ({self.journal_stuck_pattern!r})"
        return True, ""

    def restart(self) -> bool:
        """Restart the systemd unit. Returns True on success."""
        log.info("Restarting %s …", self.unit)
        try:
            result = subprocess.run(
                ["sudo", "systemctl", "restart", self.unit],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                self.last_restart_ts = time.monotonic()
                log.info("%s restarted successfully", self.unit)
                return True
            log.error("Restart of %s failed (rc=%d): %s",
                      self.unit, result.returncode, result.stderr.strip())
        except Exception:
            log.exception("Restart of %s raised an exception", self.unit)
        return False


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------

class Watchdog:
    def __init__(
        self,
        services: list[ManagedService],
        check_interval_s: float = _DEFAULT_INTERVAL_S,
        restart_cooldown_s: float = _DEFAULT_COOLDOWN_MIN * 60,
        tg_token: str = "",
        tg_chat_id: str = "",
        telegram_notify: bool = True,
    ) -> None:
        self._services = services
        self._interval = check_interval_s
        self._cooldown = restart_cooldown_s
        self._tg_token = tg_token
        self._tg_chat_id = tg_chat_id
        self._telegram_notify = telegram_notify

    def run(self) -> None:
        log.info(
            "Watchdog started — monitoring %d service(s), interval=%.0fs, cooldown=%.0fm",
            len(self._services), self._interval, self._cooldown / 60,
        )
        while True:
            try:
                self._check_all()
            except Exception:
                log.exception("Unexpected error in watchdog check loop")
            time.sleep(self._interval)

    def _check_all(self) -> None:
        for svc in self._services:
            try:
                self._check_one(svc)
            except Exception:
                log.exception("Error checking %s", svc.unit)

    def _check_one(self, svc: ManagedService) -> None:
        healthy, reason = svc.is_healthy()
        if healthy:
            if svc.consecutive_failures > 0:
                log.info("%s is healthy again", svc.unit)
                svc.consecutive_failures = 0
            return

        svc.consecutive_failures += 1
        log.warning("%s is UNHEALTHY (failure #%d): %s",
                    svc.unit, svc.consecutive_failures, reason)

        # Enforce cooldown between restarts
        elapsed = time.monotonic() - svc.last_restart_ts
        if elapsed < self._cooldown:
            remaining = int(self._cooldown - elapsed)
            log.info("%s: cooldown active — %ds remaining before next restart",
                     svc.unit, remaining)
            return

        # Attempt auto-fix
        ok = svc.restart()
        status = "✅ restarted" if ok else "❌ restart FAILED"
        msg = (
            f"🩺 Watchdog: {svc.unit}\n"
            f"Issue: {reason}\n"
            f"Failure #{svc.consecutive_failures} — {status}"
        )
        log.info("Watchdog action: %s", msg.replace("\n", " | "))
        if self._telegram_notify and self._tg_token and self._tg_chat_id:
            _tg_send(self._tg_token, self._tg_chat_id, msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        with open(_CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    return {}


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s  %(message)s",
        stream=sys.stdout,
    )

    cfg = _load_config()
    wd_cfg = cfg.get("watchdog", {})

    if not wd_cfg.get("enabled", True):
        log.info("Watchdog disabled in config — exiting")
        return 0

    interval_s      = float(wd_cfg.get("check_interval_s", _DEFAULT_INTERVAL_S))
    cooldown_min    = float(wd_cfg.get("restart_cooldown_min", _DEFAULT_COOLDOWN_MIN))
    tg_notify       = bool(wd_cfg.get("telegram_notify", True))
    stuck_threshold = int(wd_cfg.get("openclaw_stuck_threshold", _DEFAULT_STUCK_THRESHOLD))

    tg_cfg     = cfg.get("telegram", {})
    tg_token   = str(tg_cfg.get("bot_token", ""))
    tg_chat_id = str(tg_cfg.get("chat_id", ""))

    services = [
        ManagedService(
            unit="desktop-assistant-thermal.service",
        ),
        ManagedService(
            unit="desktop-assistant-core.service",
            http_check="http://localhost:8080/health",
        ),
        ManagedService(
            unit="openclaw-gateway.service",
            http_check="http://localhost:18789/health",
            journal_stuck_pattern="Bot not initialized",
            stuck_threshold=stuck_threshold,
            require_systemd_active=False,  # exits 78 when healthy instance already runs
        ),
    ]

    watchdog = Watchdog(
        services=services,
        check_interval_s=interval_s,
        restart_cooldown_s=cooldown_min * 60,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        telegram_notify=tg_notify,
    )
    watchdog.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
