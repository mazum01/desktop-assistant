"""Desktop Assistant — System Health Watchdog.

Monitors all critical services and auto-restarts any that fail or become
stuck.  Sends a Telegram notification whenever it intervenes.

Services monitored
------------------
* desktop-assistant-core    — systemctl + HTTP ping (localhost:8080/health)
* desktop-assistant-thermal — systemctl only (no HTTP interface)
* desktop-assistant-media   — systemctl --user only (no HTTP interface; music +
                              podcast playback, split out of core per
                              docs/architecture/PROCESS_ISOLATION_PROPOSAL.md
                              Phase 1). Installed as a --user unit rather than
                              system-wide because this box's passwordless sudo
                              is scoped to a fixed command list that doesn't
                              cover daemon-reload/enable for new units.
* openclaw-gateway          — systemctl + HTTP ping (localhost:18789)
                              + journal scan for stuck "Bot not initialized" loop
                              + max-uptime restart (Telegram polling-stall guard)
                              + max-uptime restart (Telegram polling-stall guard)

Restart guard
-------------
Each service has an independent cooldown (default 5 min) so a repeatedly
crashing service doesn't trigger an infinite restart storm.

Telegram notifications
----------------------
Sent via OpenClaw first (so messages appear in OpenClaw history), then
fallback to direct Bot API if OpenClaw delivery is unavailable.

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
  openclaw_max_uptime_min: 90    # max gateway uptime before forced restart (polling-stall guard)
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

import psutil
import yaml

log = logging.getLogger("watchdog")

# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

_DEFAULT_INTERVAL_S       = 30.0
_DEFAULT_COOLDOWN_MIN     = 5.0
_DEFAULT_STUCK_THRESHOLD  = 10   # "Bot not initialized" occurrences / 60 s
_DEFAULT_MAX_UPTIME_MIN   = 90   # openclaw-gateway forced restart interval (polling-stall guard)
_DEFAULT_OPENCLAW_MAX_PROCESSES = 2
_DEFAULT_STATUS_NOTIFY_MIN = 15.0
_DEFAULT_ALERT_COOLDOWN_MIN = 10.0
_DEFAULT_CORE_RSS_WARN_MB = 2200.0
_DEFAULT_CORE_FD_WARN = 900
_DEFAULT_CORE_THREADS_WARN = 180

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


def _openclaw_send(
    text: str,
    *,
    channel: str,
    target: str,
    cli_path: str = "openclaw",
) -> bool:
    if not channel or not target:
        return False
    try:
        result = subprocess.run(
            [
                cli_path, "message", "send",
                "--channel", channel,
                "--target", target,
                "--message", text,
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning("OpenClaw notify failed to execute: %s", exc)
        return False
    if result.returncode != 0:
        log.warning(
            "OpenClaw notify failed (rc=%d): %s",
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Service descriptor
# ---------------------------------------------------------------------------

@dataclass
class ManagedService:
    unit: str
    http_check: Optional[str] = None          # URL to GET; expects {"ok": true}
    http_timeout_s: float = 5.0
    process_match: Optional[str] = None       # Optional process multiplicity guard
    max_processes: int = 1
    journal_stuck_pattern: Optional[str] = None  # grep in last-60s journal
    stuck_threshold: int = _DEFAULT_STUCK_THRESHOLD
    # Set False for services that self-manage single-instance (e.g. openclaw exits
    # 78 when a healthy instance is already running, so systemd shows "inactive").
    # In that case only the HTTP check determines health.
    require_systemd_active: bool = True
    # If set, force a restart when the process owning http_check's port has been
    # running longer than this many minutes.  Guards against silent polling stalls
    # (e.g. openclaw Telegram ingress dying after a long agentic session).
    max_uptime_min: Optional[int] = None
    # True for units installed in the user's own systemd instance
    # (~/.config/systemd/user/) rather than system-wide (/etc/systemd/system/).
    # Uses `systemctl --user` (no sudo needed/possible) for both status checks
    # and restarts. desktop-assistant-media.service runs this way on boxes
    # where passwordless sudo is scoped to a fixed command list that doesn't
    # include daemon-reload/enable for new units.
    user_unit: bool = False

    # Runtime state — not part of config
    last_restart_ts: float = field(default=-1.0, init=False, repr=False)
    consecutive_failures: int = field(default=0, init=False, repr=False)
    last_failure_reason: str = field(default="", init=False, repr=False)

    def _systemctl_base(self) -> list[str]:
        return ["systemctl", "--user"] if self.user_unit else ["systemctl"]

    def _systemctl_env(self) -> Optional[dict]:
        """Environment for `systemctl --user` subprocess calls.

        The watchdog itself typically runs as a *system* unit (spawned by
        PID 1, even with User=starter) rather than inside the user's login
        session, so it has no XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS —
        `systemctl --user` fails without them ("$DBUS_SESSION_BUS_ADDRESS
        and $XDG_RUNTIME_DIR not defined"). Inject them explicitly rather
        than depend on the watchdog's own service file/ambient environment.
        """
        if not self.user_unit:
            return None
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        env.setdefault(
            "DBUS_SESSION_BUS_ADDRESS",
            f"unix:path={env['XDG_RUNTIME_DIR']}/bus",
        )
        return env

    def is_systemd_active(self) -> bool:
        try:
            result = subprocess.run(
                [*self._systemctl_base(), "is-active", self.unit],
                capture_output=True, text=True, timeout=5,
                env=self._systemctl_env(),
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

    def _matching_process_ids(self) -> list[int]:
        pattern = (self.process_match or "").strip()
        if not pattern:
            return []
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if result.returncode not in (0, 1):
            return []
        pids: list[int] = []
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                pids.append(int(line))
            except ValueError:
                continue
        return pids

    def is_process_count_healthy(self) -> bool:
        pids = self._matching_process_ids()
        if not pids:
            return True
        return len(pids) <= max(1, int(self.max_processes))

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

    def _port_from_http_check(self) -> Optional[int]:
        """Extract port number from the http_check URL."""
        if not self.http_check:
            return None
        try:
            import re
            m = re.search(r":(\d+)", self.http_check)
            return int(m.group(1)) if m else None
        except Exception:
            return None

    def _pid_for_port(self, port: int) -> Optional[int]:
        """Return PID of the process listening on the given TCP port, or None."""
        try:
            result = subprocess.run(
                ["ss", "-tlnp", f"sport = :{port}"],
                capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                # e.g. users:(("node",pid=77384,fd=25))
                import re
                m = re.search(r'pid=(\d+)', line)
                if m:
                    return int(m.group(1))
        except Exception:
            pass
        return None

    def _process_uptime_s(self, pid: int) -> Optional[float]:
        """Return how long (seconds) the given PID has been running, or None."""
        try:
            stat_path = Path(f"/proc/{pid}/stat")
            if not stat_path.exists():
                return None
            stat = stat_path.read_text().split()
            starttime_ticks = int(stat[21])
            clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
            boot_time_s: float = 0.0
            with open("/proc/stat") as f:
                for line in f:
                    if line.startswith("btime "):
                        boot_time_s = float(line.split()[1])
                        break
            start_epoch = boot_time_s + starttime_ticks / clk_tck
            return time.time() - start_epoch
        except Exception:
            return None

    def is_uptime_exceeded(self) -> bool:
        """Return True if the process owning our port has exceeded max_uptime_min."""
        if not self.max_uptime_min:
            return False
        port = self._port_from_http_check()
        if not port:
            return False
        pid = self._pid_for_port(port)
        if not pid:
            return False
        uptime = self._process_uptime_s(pid)
        if uptime is None:
            return False
        limit_s = self.max_uptime_min * 60
        if uptime >= limit_s:
            log.info("%s: process pid=%d uptime=%.0fm ≥ limit=%dm — forcing refresh",
                     self.unit, pid, uptime / 60, self.max_uptime_min)
            return True
        return False

    def is_healthy(self) -> tuple[bool, str]:
        """Return (healthy, reason). reason is '' when healthy."""
        if self.require_systemd_active and not self.is_systemd_active():
            return False, "systemd unit inactive/failed"
        if not self.is_process_count_healthy():
            count = len(self._matching_process_ids())
            return False, f"process multiplicity detected ({count} matches for '{self.process_match}')"
        if not self.is_http_healthy():
            return False, f"HTTP health check failed ({self.http_check})"
        if self.is_journal_stuck():
            return False, f"stuck loop detected ({self.journal_stuck_pattern!r})"
        if self.is_uptime_exceeded():
            return False, f"max uptime {self.max_uptime_min}m exceeded (polling-stall guard)"
        return True, ""

    def _systemd_main_pid(self) -> Optional[int]:
        """Return the MainPID systemd has recorded for this unit, or None."""
        try:
            result = subprocess.run(
                [*self._systemctl_base(), "show", self.unit, "-p", "MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
                env=self._systemctl_env(),
            )
            pid = int(result.stdout.strip() or 0)
            return pid or None
        except Exception:
            return None

    def _kill_orphan_port_holder(self) -> None:
        """If the port is held by a process systemd doesn't manage, kill it.

        OpenClaw can be started manually (e.g. ``openclaw gateway`` from a shell);
        when that instance stalls, the systemd unit's ``ExecStart`` keeps exiting
        78 ("another instance is healthy") and ``systemctl restart`` becomes a
        no-op for the actual port holder.  Detect that case and terminate the
        orphan first so the next systemd start can bind the port.
        """
        port = self._port_from_http_check()
        if not port:
            return
        port_pid = self._pid_for_port(port)
        if not port_pid:
            return
        main_pid = self._systemd_main_pid()
        if main_pid and port_pid == main_pid:
            return  # systemd already owns it; normal restart will handle it
        log.warning(
            "%s: port %d is held by orphan pid=%d (systemd MainPID=%s) — terminating",
            self.unit, port, port_pid, main_pid,
        )
        try:
            subprocess.run(["sudo", "kill", str(port_pid)],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            log.exception("%s: failed to send SIGTERM to orphan pid=%d",
                          self.unit, port_pid)
            return
        for _ in range(20):  # up to ~5 s
            time.sleep(0.25)
            if not Path(f"/proc/{port_pid}").exists():
                log.info("%s: orphan pid=%d exited cleanly", self.unit, port_pid)
                return
        log.warning("%s: orphan pid=%d did not exit — escalating to SIGKILL",
                    self.unit, port_pid)
        try:
            subprocess.run(["sudo", "kill", "-9", str(port_pid)],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            log.exception("%s: failed to SIGKILL orphan pid=%d",
                          self.unit, port_pid)

    def _kill_extra_processes(self) -> None:
        pids = self._matching_process_ids()
        allowed = max(1, int(self.max_processes))
        if len(pids) <= allowed:
            return
        keep_pid = self._systemd_main_pid()
        for pid in pids:
            if keep_pid and pid == keep_pid:
                continue
            try:
                os.kill(pid, 15)
            except OSError:
                continue

    def restart(self) -> bool:
        """Restart the systemd unit. Returns True on success."""
        log.info("Restarting %s …", self.unit)
        self._kill_extra_processes()
        self._kill_orphan_port_holder()
        # User-manager units restart via the user's own systemd instance —
        # no sudo needed (and none of our fixed passwordless sudo entries
        # cover arbitrary units anyway).
        cmd = [*self._systemctl_base(), "restart", self.unit] if self.user_unit \
            else ["sudo", "systemctl", "restart", self.unit]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=30,
                env=self._systemctl_env(),
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
        notify_via_openclaw: bool = True,
        openclaw_notify_channel: str = "telegram",
        openclaw_notify_target: str = "",
        openclaw_cli_path: str = "openclaw",
        status_notify_interval_s: float = _DEFAULT_STATUS_NOTIFY_MIN * 60.0,
        alert_cooldown_s: float = _DEFAULT_ALERT_COOLDOWN_MIN * 60.0,
        core_rss_warn_mb: float = _DEFAULT_CORE_RSS_WARN_MB,
        core_fd_warn: int = _DEFAULT_CORE_FD_WARN,
        core_threads_warn: int = _DEFAULT_CORE_THREADS_WARN,
    ) -> None:
        self._services = services
        self._interval = check_interval_s
        self._cooldown = restart_cooldown_s
        self._tg_token = tg_token
        self._tg_chat_id = tg_chat_id
        self._telegram_notify = telegram_notify
        self._notify_via_openclaw = notify_via_openclaw
        self._openclaw_notify_channel = (openclaw_notify_channel or "").strip()
        self._openclaw_notify_target = (openclaw_notify_target or "").strip()
        self._openclaw_cli_path = (openclaw_cli_path or "openclaw").strip()
        self._status_notify_interval_s = max(0.0, status_notify_interval_s)
        self._alert_cooldown_s = max(10.0, alert_cooldown_s)
        self._core_rss_warn_mb = core_rss_warn_mb
        self._core_fd_warn = max(1, core_fd_warn)
        self._core_threads_warn = max(1, core_threads_warn)
        self._last_status_notify_ts = 0.0
        self._last_alert_ts = 0.0

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
        self._maybe_send_status_heartbeat()
        self._maybe_send_resource_alerts()

    def _notify(self, msg: str) -> None:
        if not self._telegram_notify:
            return
        if self._notify_via_openclaw:
            delivered = _openclaw_send(
                msg,
                channel=self._openclaw_notify_channel,
                target=self._openclaw_notify_target,
                cli_path=self._openclaw_cli_path,
            )
            if delivered:
                return
        if self._tg_token and self._tg_chat_id:
            _tg_send(self._tg_token, self._tg_chat_id, msg)

    def _check_one(self, svc: ManagedService) -> None:
        healthy, reason = svc.is_healthy()
        if healthy:
            if svc.consecutive_failures > 0:
                log.info("%s is healthy again", svc.unit)
                self._notify(f"✅ Watchdog recovery: {svc.unit} is healthy again.")
                svc.consecutive_failures = 0
                svc.last_failure_reason = ""
            return

        svc.consecutive_failures += 1
        if svc.consecutive_failures == 1 or reason != svc.last_failure_reason:
            log.warning("%s is UNHEALTHY (failure #%d): %s",
                        svc.unit, svc.consecutive_failures, reason)
            self._notify(
                f"⚠️ Watchdog alert: {svc.unit}\n"
                f"Issue: {reason}\n"
                f"Failure #{svc.consecutive_failures}"
            )
        svc.last_failure_reason = reason

        # Enforce cooldown between restarts
        elapsed = time.monotonic() - svc.last_restart_ts if svc.last_restart_ts > 0 else self._cooldown + 1
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
        self._notify(msg)

    def _core_pid(self) -> Optional[int]:
        try:
            result = subprocess.run(
                ["systemctl", "show", "desktop-assistant-core.service", "-p", "MainPID", "--value"],
                capture_output=True, text=True, timeout=5,
            )
            pid = int(result.stdout.strip() or 0)
            return pid or None
        except (subprocess.SubprocessError, ValueError, OSError):
            return None

    def _maybe_send_status_heartbeat(self) -> None:
        if self._status_notify_interval_s <= 0:
            return
        now = time.monotonic()
        if self._last_status_notify_ts and (now - self._last_status_notify_ts) < self._status_notify_interval_s:
            return
        statuses = []
        for svc in self._services:
            healthy, reason = svc.is_healthy()
            statuses.append(f"{svc.unit}: {'ok' if healthy else 'bad'}{'' if healthy else f' ({reason})'}")
        pid = self._core_pid()
        metrics = ""
        if pid:
            try:
                p = psutil.Process(pid)
                rss_mb = p.memory_info().rss / 1024 / 1024
                fds = p.num_fds()
                threads = p.num_threads()
                metrics = f"\ncore pid={pid} rss={rss_mb:.0f}MB fds={fds} threads={threads}"
            except (psutil.Error, OSError, ValueError):
                pass
        self._notify("📊 Watchdog status heartbeat\n" + "\n".join(statuses) + metrics)
        self._last_status_notify_ts = now

    def _maybe_send_resource_alerts(self) -> None:
        now = time.monotonic()
        if self._last_alert_ts and (now - self._last_alert_ts) < self._alert_cooldown_s:
            return
        pid = self._core_pid()
        if not pid:
            return
        try:
            p = psutil.Process(pid)
            rss_mb = p.memory_info().rss / 1024 / 1024
            fds = p.num_fds()
            threads = p.num_threads()
        except (psutil.Error, OSError, ValueError):
            return
        breaches = []
        if rss_mb >= self._core_rss_warn_mb:
            breaches.append(f"rss {rss_mb:.0f}MB ≥ {self._core_rss_warn_mb:.0f}MB")
        if fds >= self._core_fd_warn:
            breaches.append(f"fds {fds} ≥ {self._core_fd_warn}")
        if threads >= self._core_threads_warn:
            breaches.append(f"threads {threads} ≥ {self._core_threads_warn}")
        if not breaches:
            return
        self._notify(
            "🚨 Watchdog resource alert: desktop-assistant-core\n"
            f"pid={pid}\n" + "\n".join(breaches)
        )
        self._last_alert_ts = now


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

    interval_s        = float(wd_cfg.get("check_interval_s", _DEFAULT_INTERVAL_S))
    cooldown_min      = float(wd_cfg.get("restart_cooldown_min", _DEFAULT_COOLDOWN_MIN))
    tg_notify         = bool(wd_cfg.get("telegram_notify", True))
    stuck_threshold   = int(wd_cfg.get("openclaw_stuck_threshold", _DEFAULT_STUCK_THRESHOLD))
    max_uptime_min    = int(wd_cfg.get("openclaw_max_uptime_min", _DEFAULT_MAX_UPTIME_MIN))
    openclaw_max_processes = int(wd_cfg.get("openclaw_max_processes", _DEFAULT_OPENCLAW_MAX_PROCESSES))
    status_notify_min = float(wd_cfg.get("status_notify_interval_min", _DEFAULT_STATUS_NOTIFY_MIN))
    alert_cooldown_min = float(wd_cfg.get("alert_cooldown_min", _DEFAULT_ALERT_COOLDOWN_MIN))
    core_rss_warn_mb = float(wd_cfg.get("core_rss_warn_mb", _DEFAULT_CORE_RSS_WARN_MB))
    core_fd_warn = int(wd_cfg.get("core_fd_warn", _DEFAULT_CORE_FD_WARN))
    core_threads_warn = int(wd_cfg.get("core_threads_warn", _DEFAULT_CORE_THREADS_WARN))
    notify_via_openclaw = bool(wd_cfg.get("notify_via_openclaw", True))
    openclaw_notify_channel = str(wd_cfg.get("openclaw_notify_channel", "telegram"))
    openclaw_notify_target = str(wd_cfg.get("openclaw_notify_target", ""))
    openclaw_cli_path = str(wd_cfg.get("openclaw_cli_path", "openclaw"))

    tg_cfg     = cfg.get("telegram", {})
    tg_token   = str(tg_cfg.get("bot_token", ""))
    tg_chat_id = str(tg_cfg.get("chat_id", ""))
    if not openclaw_notify_target:
        openclaw_notify_target = tg_chat_id

    services = [
        ManagedService(
            unit="desktop-assistant-thermal.service",
        ),
        ManagedService(
            unit="desktop-assistant-core.service",
            http_check="http://localhost:8080/health",
        ),
        ManagedService(
            unit="desktop-assistant-media.service",
            # This box's passwordless sudo doesn't cover daemon-reload/enable
            # for new units, so media runs as a --user unit; see the
            # user_unit field docstring above.
            user_unit=True,
        ),
        ManagedService(
            unit="openclaw-gateway.service",
            http_check="http://localhost:18789/health",
            process_match="openclaw/dist/index.js gateway",
            max_processes=max(1, openclaw_max_processes),
            journal_stuck_pattern="Bot not initialized",
            stuck_threshold=stuck_threshold,
            require_systemd_active=bool(wd_cfg.get("openclaw_require_systemd_active", True)),
            max_uptime_min=max_uptime_min,
        ),
    ]

    watchdog = Watchdog(
        services=services,
        check_interval_s=interval_s,
        restart_cooldown_s=cooldown_min * 60,
        tg_token=tg_token,
        tg_chat_id=tg_chat_id,
        telegram_notify=tg_notify,
        notify_via_openclaw=notify_via_openclaw,
        openclaw_notify_channel=openclaw_notify_channel,
        openclaw_notify_target=openclaw_notify_target,
        openclaw_cli_path=openclaw_cli_path,
        status_notify_interval_s=status_notify_min * 60,
        alert_cooldown_s=alert_cooldown_min * 60,
        core_rss_warn_mb=core_rss_warn_mb,
        core_fd_warn=core_fd_warn,
        core_threads_warn=core_threads_warn,
    )
    watchdog.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
