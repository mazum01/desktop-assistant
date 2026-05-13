"""SkillsService — dispatches ``av.utterance`` events to the :class:`SkillRegistry`.

Subscribes to the shared ``av.utterance`` bus topic; any component that
performs speech recognition (STT pipeline, web-UI command box, etc.) should
publish on that topic with the payload ``{"text": "<utterance>"}``.

``AVService`` also subscribes to ``av.utterance`` for version-query handling;
both subscribers receive every event — there is no conflict because no skill
pattern overlaps with the version-query regex in ``VersionAnnouncer``.
"""

from __future__ import annotations

import logging

from src.core.service import Service
from src.skills.base import SkillRegistry
from src.skills.describe_scene import DescribeSceneSkill
from src.skills.face_tracking_toggle import FaceTrackingToggleSkill
from src.skills.greeting import GreetingSkill
from src.skills.meet_face import MeetFaceSkill
from src.skills.motion_control import MotionControlSkill
from src.skills.music_control import MusicControlSkill
from src.skills.object_detect_toggle import ObjectDetectToggleSkill
from src.skills.tell_joke import TellJokeSkill
from src.skills.tell_time import TellTimeSkill

log = logging.getLogger(__name__)


class SkillsService(Service):
    """Voice-intent dispatch service."""

    def __init__(self, bus) -> None:
        super().__init__(bus)
        self._registry = SkillRegistry()
        self._build_registry()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_registry(self) -> None:
        """Register skills in priority order (first match wins)."""
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
        ]:
            self._registry.register(skill)
        log.info("SkillsService: %d skills registered: %s",
                 len(self._registry.skill_names),
                 ", ".join(self._registry.skill_names))

    # ------------------------------------------------------------------
    # Service lifecycle
    # ------------------------------------------------------------------

    def on_start(self) -> None:
        self.bus.subscribe("av.utterance", self._on_utterance)
        log.info("SkillsService started.")

    def on_stop(self) -> None:
        log.info("SkillsService stopped.")

    # ------------------------------------------------------------------
    # Bus handlers
    # ------------------------------------------------------------------

    def _on_utterance(self, payload: dict) -> None:
        text = payload.get("text", "").strip()
        if not text:
            return
        matched = self._registry.dispatch(text, self.bus)
        if not matched:
            log.debug("SkillsService: no skill matched %r", text)
