"""Tests for the system health watchdog."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from src.watchdog.watchdog import ManagedService, Watchdog


# ---------------------------------------------------------------------------
# ManagedService — unit health checks
# ---------------------------------------------------------------------------

class TestManagedServiceSystemd:
    def test_active_service_is_healthy(self):
        svc = ManagedService(unit="fake.service")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            assert svc.is_systemd_active() is True

    def test_inactive_service_not_healthy(self):
        svc = ManagedService(unit="fake.service")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=3)
            assert svc.is_systemd_active() is False

    def test_subprocess_exception_returns_false(self):
        svc = ManagedService(unit="fake.service")
        with patch("subprocess.run", side_effect=OSError("no systemctl")):
            assert svc.is_systemd_active() is False


class TestManagedServiceHttp:
    def test_no_http_check_always_healthy(self):
        svc = ManagedService(unit="fake.service")
        assert svc.is_http_healthy() is True

    def test_ok_true_response_healthy(self):
        svc = ManagedService(unit="fake.service", http_check="http://localhost:9999/health")
        with patch("urllib.request.urlopen") as mock_open:
            mock_resp = MagicMock()
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_resp.read.return_value = b'{"ok": true}'
            mock_open.return_value = mock_resp
            assert svc.is_http_healthy() is True

    def test_network_error_unhealthy(self):
        import urllib.error
        svc = ManagedService(unit="fake.service", http_check="http://localhost:9999/health")
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert svc.is_http_healthy() is False


class TestManagedServiceJournalStuck:
    def test_no_pattern_never_stuck(self):
        svc = ManagedService(unit="fake.service")
        assert svc.is_journal_stuck() is False

    def test_under_threshold_not_stuck(self):
        svc = ManagedService(unit="fake.service",
                             journal_stuck_pattern="Bot not initialized",
                             stuck_threshold=10)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                stdout="Bot not initialized\nBot not initialized\n",
                returncode=0,
            )
            assert svc.is_journal_stuck() is False  # only 2 occurrences

    def test_over_threshold_is_stuck(self):
        svc = ManagedService(unit="fake.service",
                             journal_stuck_pattern="Bot not initialized",
                             stuck_threshold=3)
        text = "Bot not initialized\n" * 5
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=text, returncode=0)
            assert svc.is_journal_stuck() is True


# ---------------------------------------------------------------------------
# ManagedService.restart
# ---------------------------------------------------------------------------

class TestManagedServiceRestart:
    def test_successful_restart_updates_timestamp(self):
        svc = ManagedService(unit="fake.service")
        before = time.monotonic()
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            ok = svc.restart()
        assert ok is True
        assert svc.last_restart_ts >= before

    def test_failed_restart_returns_false(self):
        svc = ManagedService(unit="fake.service")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="error")
            ok = svc.restart()
        assert ok is False


# ---------------------------------------------------------------------------
# Watchdog cooldown
# ---------------------------------------------------------------------------

class TestWatchdogCooldown:
    def _make_unhealthy_svc(self):
        svc = ManagedService(unit="fake.service")
        svc.is_healthy = MagicMock(return_value=(False, "unit inactive"))
        svc.restart = MagicMock(return_value=True)
        return svc

    def test_first_failure_triggers_restart(self):
        svc = self._make_unhealthy_svc()
        # Avoid dependence on host uptime: ensure cooldown has already elapsed.
        svc.last_restart_ts = time.monotonic() - 301
        wd = Watchdog([svc], check_interval_s=1, restart_cooldown_s=300,
                      telegram_notify=False)
        wd._check_one(svc)
        svc.restart.assert_called_once()

    def test_second_failure_within_cooldown_skips_restart(self):
        svc = self._make_unhealthy_svc()
        svc.last_restart_ts = time.monotonic()  # just restarted
        wd = Watchdog([svc], check_interval_s=1, restart_cooldown_s=300,
                      telegram_notify=False)
        wd._check_one(svc)
        svc.restart.assert_not_called()

    def test_failure_after_cooldown_restarts_again(self):
        svc = self._make_unhealthy_svc()
        svc.last_restart_ts = time.monotonic() - 400  # cooldown expired
        wd = Watchdog([svc], check_interval_s=1, restart_cooldown_s=300,
                      telegram_notify=False)
        wd._check_one(svc)
        svc.restart.assert_called_once()

    def test_initial_state_does_not_apply_startup_cooldown(self):
        svc = self._make_unhealthy_svc()
        svc.last_restart_ts = -1
        wd = Watchdog([svc], check_interval_s=1, restart_cooldown_s=300,
                      telegram_notify=False)
        wd._check_one(svc)
        svc.restart.assert_called_once()


# ---------------------------------------------------------------------------
# Watchdog Telegram notification
# ---------------------------------------------------------------------------

class TestWatchdogTelegramNotify:
    def test_notify_sent_on_restart(self):
        svc = ManagedService(unit="fake.service")
        svc.is_healthy = MagicMock(return_value=(False, "dead"))
        svc.restart = MagicMock(return_value=True)
        wd = Watchdog([svc], restart_cooldown_s=0,
                      tg_token="TOKEN", tg_chat_id="CHAT", telegram_notify=True)
        with patch("src.watchdog.watchdog._tg_send") as mock_tg:
            wd._check_one(svc)
            assert mock_tg.call_count >= 1
            args = mock_tg.call_args_list[-1][0]
            assert args[0] == "TOKEN"
            assert args[1] == "CHAT"
            assert "fake.service" in args[2]

    def test_no_notify_when_disabled(self):
        svc = ManagedService(unit="fake.service")
        svc.is_healthy = MagicMock(return_value=(False, "dead"))
        svc.restart = MagicMock(return_value=True)
        wd = Watchdog([svc], restart_cooldown_s=0,
                      tg_token="TOKEN", tg_chat_id="CHAT", telegram_notify=False)
        with patch("src.watchdog.watchdog._tg_send") as mock_tg:
            wd._check_one(svc)
            mock_tg.assert_not_called()
