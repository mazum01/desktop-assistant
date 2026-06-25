"""
Tests for PrivacyService and NudityDetector.
"""
import numpy as np
import pytest
from unittest.mock import MagicMock, patch


# ── NudityDetector ────────────────────────────────────────────────────────────

class TestNudityDetectorSimMode:
    """NudityDetector must degrade gracefully when nudenet is unavailable."""

    def _make_detector(self, threshold=0.6):
        with patch.dict("sys.modules", {"nudenet": None}):
            from src.perception.nudity_detector import NudityDetector  # type: ignore
            return NudityDetector(threshold=threshold)

    def test_sim_mode_hardware_not_ready(self):
        det = self._make_detector()
        assert det.hardware_ready is False

    def test_sim_mode_is_explicit_returns_false(self):
        det = self._make_detector()
        frame = np.zeros((480, 640, 3), dtype=np.uint8)
        explicit, detections = det.is_explicit(frame)
        assert explicit is False
        assert detections == []


# ── PrivacyService ────────────────────────────────────────────────────────────

class TestPrivacyServiceInit:
    def _make_svc(self, enabled=True):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        cfg = PrivacyConfig(enabled=enabled)
        svc = PrivacyService(bus=None, vision_service=None, config=cfg)
        return svc

    def test_default_not_looking_away(self):
        svc = self._make_svc()
        assert svc._looking_away is False

    def test_enabled_flag_from_config(self):
        svc = self._make_svc(enabled=False)
        assert svc._enabled is False


class TestPrivacyServiceBusMessages:
    """Test look-away and resume publish the right topics."""

    def _make_svc_with_bus(self, enabled=True):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        cfg = PrivacyConfig(
            enabled=enabled,
            look_away_angle_deg=45.0,
            cooldown_s=1.0,
            clear_frames=2,
            announce=True,
            announce_text="Privacy please.",
        )
        svc = PrivacyService(bus=bus, vision_service=None, config=cfg)
        return svc, bus

    def test_look_away_publishes_pan_and_disables_tracking(self):
        svc, bus = self._make_svc_with_bus()
        svc._look_away()
        published_topics = {call.args[0] for call in bus.publish.call_args_list}
        assert "motion.set_enabled" in published_topics
        assert "motion.pan_to" in published_topics
        assert "privacy.looking_away" in published_topics
        assert "av.say" in published_topics

    def test_look_away_sets_looking_away_flag(self):
        svc, bus = self._make_svc_with_bus()
        svc._look_away()
        assert svc._looking_away is True

    def test_resume_publishes_motion_enabled_and_resuming(self):
        svc, bus = self._make_svc_with_bus()
        svc._looking_away = True
        svc._resume()
        published_topics = {call.args[0] for call in bus.publish.call_args_list}
        assert "motion.set_enabled" in published_topics
        assert "privacy.resuming" in published_topics

    def test_resume_clears_looking_away_flag(self):
        svc, bus = self._make_svc_with_bus()
        svc._looking_away = True
        svc._resume()
        assert svc._looking_away is False


class TestPrivacyServiceClearFrames:
    """Clear-frames threshold: must see N consecutive clean frames before resuming."""

    def _make_svc(self, clear_frames=3):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        cfg = PrivacyConfig(
            enabled=True,
            clear_frames=clear_frames,
            cooldown_s=0.0,  # no delay in tests
        )
        svc = PrivacyService(bus=bus, vision_service=None, config=cfg)
        return svc, bus

    def test_does_not_resume_before_threshold(self):
        """After looking away, fewer than clear_frames clean results don't trigger resume."""
        import time
        svc, bus = self._make_svc(clear_frames=3)
        svc._looking_away = True
        svc._cooldown_until = time.monotonic() - 1  # past cooldown

        with patch.object(svc, "_resume") as mock_resume:
            # feed only 2 clean frames (threshold is 3)
            for _ in range(2):
                svc._clear_streak += 1
                if svc._clear_streak >= svc._cfg.clear_frames:
                    if time.monotonic() >= svc._cooldown_until:
                        svc._resume()
            mock_resume.assert_not_called()

    def test_resumes_at_threshold(self):
        import time
        svc, bus = self._make_svc(clear_frames=3)
        svc._looking_away = True
        svc._cooldown_until = time.monotonic() - 1  # past cooldown

        with patch.object(svc, "_resume") as mock_resume:
            for _ in range(3):
                svc._clear_streak += 1
                if svc._clear_streak >= svc._cfg.clear_frames:
                    if time.monotonic() >= svc._cooldown_until:
                        svc._resume()
            mock_resume.assert_called_once()


class TestPrivacyServiceSetEnabled:
    """privacy.set_enabled=False while looking away should trigger resume."""

    def test_disable_while_looking_away_calls_resume(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        svc = PrivacyService(bus=bus, config=PrivacyConfig(enabled=True))
        svc._looking_away = True

        with patch.object(svc, "_resume") as mock_resume:
            svc._on_set_enabled("privacy.set_enabled", {"enabled": False})
            mock_resume.assert_called_once()

    def test_disable_updates_enabled_flag(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        svc = PrivacyService(bus=bus, config=PrivacyConfig(enabled=True))
        svc._on_set_enabled("privacy.set_enabled", {"enabled": False})
        assert svc._enabled is False


class TestPrivacyServiceSetConfig:
    def test_set_config_updates_fields_and_resumes_if_disabled(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        svc = PrivacyService(bus=bus, config=PrivacyConfig(enabled=True))
        svc._looking_away = True

        with patch.object(svc, "_resume") as mock_resume:
            svc._on_set_config("privacy.set_config", {
                "enabled": False,
                "rate_hz": 2.0,
                "idle_rate_hz": 0.5,
                "threshold": 0.7,
                "look_away_angle_deg": 60.0,
                "cooldown_s": 5.0,
                "clear_frames": 4,
                "require_person": True,
                "person_hold_s": 6.0,
                "announce": False,
                "announce_text": "privacy mode",
                "resume_text": "all clear",
            })

            assert svc._enabled is False
            assert svc._cfg.rate_hz == pytest.approx(2.0)
            assert svc._cfg.idle_rate_hz == pytest.approx(0.5)
            assert svc._cfg.threshold == pytest.approx(0.7)
            assert svc._cfg.look_away_angle_deg == pytest.approx(60.0)
            assert svc._cfg.cooldown_s == pytest.approx(5.0)
            assert svc._cfg.clear_frames == 4
            assert svc._cfg.require_person is True
            assert svc._cfg.person_hold_s == pytest.approx(6.0)
            assert svc._cfg.announce is False
            assert svc._cfg.announce_text == "privacy mode"
            assert svc._cfg.resume_text == "all clear"
            mock_resume.assert_called_once()

    def test_set_config_clamps_ranges(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        bus = MagicMock()
        svc = PrivacyService(bus=bus, config=PrivacyConfig(enabled=True))
        svc._on_set_config("privacy.set_config", {
            "rate_hz": 0.0,
            "idle_rate_hz": 0.0,
            "threshold": 9.0,
            "cooldown_s": -1.0,
            "clear_frames": 0,
            "person_hold_s": 0.0,
        })
        assert svc._cfg.rate_hz == pytest.approx(0.1)
        assert svc._cfg.idle_rate_hz == pytest.approx(0.05)
        assert svc._cfg.threshold == pytest.approx(1.0)
        assert svc._cfg.cooldown_s == pytest.approx(0.0)
        assert svc._cfg.clear_frames == 1
        assert svc._cfg.person_hold_s == pytest.approx(0.5)


class TestPrivacyServiceEfficiency:
    def test_requires_recent_person_when_enabled(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        cfg = PrivacyConfig(enabled=True, require_person=True, person_hold_s=5.0)
        svc = PrivacyService(bus=MagicMock(), vision_service=MagicMock(), config=cfg)
        svc._last_person_seen_ts = 0.0
        assert svc._should_run_detection(now=100.0) is False

    def test_runs_when_person_recent(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        cfg = PrivacyConfig(enabled=True, require_person=True, person_hold_s=5.0)
        svc = PrivacyService(bus=MagicMock(), vision_service=MagicMock(), config=cfg)
        svc._last_person_seen_ts = 97.0
        assert svc._should_run_detection(now=100.0) is True

    def test_effective_rate_uses_idle_without_recent_person(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        cfg = PrivacyConfig(enabled=True, rate_hz=1.0, idle_rate_hz=0.25, require_person=True, person_hold_s=5.0)
        svc = PrivacyService(bus=MagicMock(), vision_service=MagicMock(), config=cfg)
        svc._last_person_seen_ts = 0.0
        assert svc._effective_rate_hz(now=100.0) == pytest.approx(0.25)

    def test_effective_rate_uses_active_with_recent_person(self):
        from src.services.privacy_service import PrivacyService, PrivacyConfig
        cfg = PrivacyConfig(enabled=True, rate_hz=1.0, idle_rate_hz=0.25, require_person=True, person_hold_s=5.0)
        svc = PrivacyService(bus=MagicMock(), vision_service=MagicMock(), config=cfg)
        svc._last_person_seen_ts = 99.0
        assert svc._effective_rate_hz(now=100.0) == pytest.approx(1.0)
