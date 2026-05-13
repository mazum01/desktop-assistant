"""SkillsService — dispatches ``av.utterance`` events to the :class:`SkillRegistry`.

Subscribes to the shared ``av.utterance`` bus topic; any component that
performs speech recognition (STT pipeline, web-UI command box, etc.) should
publish on that topic with the payload ``{"text": "<utterance>"}``.

``AVService`` also subscribes to ``av.utterance`` for version-query handling;
both subscribers receive every event — there is no conflict because no skill
pattern overlaps with the version-query regex in ``VersionAnnouncer``.

Topics subscribed
-----------------
av.utterance        ``{"text": str}``
thermal.temp        ``{"celsius": float}``  — feeds SystemStatusSkill live data
av.spoke            (used to update cpu_percent in live_data)
"""

from __future__ import annotations

import logging

import psutil

from src.core.service import Service
from src.skills.base import SkillRegistry
from src.skills.describe_scene import DescribeSceneSkill
from src.skills.face_tracking_toggle import FaceTrackingToggleSkill
from src.skills.greeting import GreetingSkill
from src.skills.help_skill import HelpSkill
from src.skills.meet_face import MeetFaceSkill
from src.skills.motion_control import MotionControlSkill
from src.skills.music_control import MusicControlSkill
from src.skills.news_skill import NewsSkill
from src.skills.object_detect_toggle import ObjectDetectToggleSkill
from src.skills.quiet_hours_skill import QuietHoursSkill
from src.skills.reminder_skill import ReminderSkill
from src.skills.smart_home_skill import SmartHomeSkill
from src.skills.system_status_skill import SystemStatusSkill
from src.skills.tell_joke import TellJokeSkill
from src.skills.tell_time import TellTimeSkill
from src.skills.volume_skill import VolumeSkill
from src.skills.weather_skill import WeatherSkill

log = logging.getLogger(__name__)


class SkillsService(Service):
    """Voice-intent dispatch service."""

    def __init__(self, bus, quiet_hours=None) -> None:
        super().__init__(bus)
        self._quiet_hours = quiet_hours
        # Shared live telemetry dict injected into SystemStatusSkill
        self._live_data: dict = {}
        self._registry = SkillRegistry()
        self._build_registry()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def registry(self) -> SkillRegistry:
        return self._registry

    def find_skill(self, name: str):
        """Return the skill with *name*, or None."""
        return self._registry.find(name)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_registry(self) -> None:
        """Register skills in priority order (first match wins)."""
        self._reminder_skill = ReminderSkill()
        for skill in [
            GreetingSkill(),
            TellTimeSkill(),
            TellJokeSkill(),
            MeetFaceSkill(),
            DescribeSceneSkill(),
            MotionControlSkill(),
            MusicControlSkill(),
            ObjectDetectToggleSkill(),
            FaceTrackingToggleSkill(),
            VolumeSkill(),
            WeatherSkill(),
            self._reminder_skill,
            NewsSkill(),
            SmartHomeSkill(),
            QuietHoursSkill(quiet_hours=self._quiet_hours),
            SystemStatusSkill(live_data=self._live_data),
            HelpSkill(),
        ]:
            self._registry.register(skill)
        log.info("SkillsService: %d skills registered: %s",
                 len(self._registry.skill_names),
                 ", ".join(self._registry.skill_names))

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.bus.subscribe("av.utterance",  self._on_utterance)
        self.bus.subscribe("thermal.temp",  self._on_thermal)
        # Start skills that have background threads
        self._reminder_skill.start(self.bus)
        log.info("SkillsService started.")

    def on_stop(self) -> None:
        self._reminder_skill.stop()
        log.info("SkillsService stopped.")

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------

    def _on_utterance(self, payload: dict) -> None:
        text = payload.get("text", "").strip()
        if not text:
            return
        # Refresh CPU before SystemStatusSkill might be dispatched
        self._live_data["cpu_percent"] = psutil.cpu_percent(interval=None)
        matched = self._registry.dispatch(text, self.bus)
        if not matched:
            log.debug("SkillsService: no skill matched %r", text)

    def _on_thermal(self, _topic, payload) -> None:
        if isinstance(payload, dict):
            if "celsius" in payload:
                self._live_data["temperature"] = float(payload["celsius"])
            if "fan_duty" in payload:
                self._live_data["fan_duty"] = float(payload["fan_duty"])
