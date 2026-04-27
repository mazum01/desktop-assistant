"""Unit tests for src/audio/tts.py and src/audio/version_announcer.py."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.audio.tts import TTSConfig, TextToSpeech
from src.audio.version_announcer import (
    VersionAnnouncer,
    is_version_query,
)


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

class TestTextToSpeech:
    def test_sim_when_binary_missing(self):
        with patch("src.audio.tts.shutil.which", return_value=None):
            tts = TextToSpeech()
        assert tts.hardware_ready is False
        # say() in sim mode must not raise
        tts.say("hello world")

    def test_say_invokes_espeak(self):
        with patch("src.audio.tts.shutil.which", return_value="/usr/bin/espeak-ng"), \
             patch("src.audio.tts.subprocess.run") as mock_run:
            tts = TextToSpeech()
            tts.say("hello")
        assert mock_run.called
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "/usr/bin/espeak-ng"
        assert "hello" in cmd

    def test_voice_and_speed_passed_to_espeak(self):
        cfg = TTSConfig(voice="en-gb", speed_wpm=180, pitch=60, amplitude=120)
        with patch("src.audio.tts.shutil.which", return_value="/usr/bin/espeak-ng"), \
             patch("src.audio.tts.subprocess.run") as mock_run:
            tts = TextToSpeech(cfg)
            tts.say("test")
        cmd = mock_run.call_args[0][0]
        assert "en-gb" in cmd
        assert "180" in cmd
        assert "60" in cmd
        assert "120" in cmd

    def test_render_to_wav_uses_w_flag(self):
        with patch("src.audio.tts.shutil.which", return_value="/usr/bin/espeak-ng"), \
             patch("src.audio.tts.subprocess.run") as mock_run:
            tts = TextToSpeech()
            tts.render_to_wav("hi", "/tmp/x.wav")
        cmd = mock_run.call_args[0][0]
        assert "-w" in cmd
        assert "/tmp/x.wav" in cmd


# ---------------------------------------------------------------------------
# is_version_query
# ---------------------------------------------------------------------------

class TestIsVersionQuery:
    @pytest.mark.parametrize("phrase", [
        "what is your version",
        "What's your version?",
        "what version are you",
        "tell me your version",
        "Tell me the version",
        "give me the version number",
        "which version are you running",
    ])
    def test_positive(self, phrase):
        assert is_version_query(phrase) is True

    @pytest.mark.parametrize("phrase", [
        "",
        "what's the weather",
        "set a timer",
        "version control is great",  # word "version" alone shouldn't match
    ])
    def test_negative(self, phrase):
        assert is_version_query(phrase) is False


# ---------------------------------------------------------------------------
# VersionAnnouncer
# ---------------------------------------------------------------------------

class TestVersionAnnouncer:
    def test_speak_version_calls_tts(self):
        fake_tts = MagicMock()
        fake_tts.hardware_ready = True
        ann = VersionAnnouncer(tts=fake_tts)
        ann.speak_version()
        assert fake_tts.say.called
        spoken_text = fake_tts.say.call_args[0][0]
        assert "version" in spoken_text.lower()

    def test_announce_startup_includes_starting_phrase(self):
        fake_tts = MagicMock()
        ann = VersionAnnouncer(tts=fake_tts)
        ann.announce_startup()
        text = fake_tts.say.call_args[0][0].lower()
        assert "starting" in text
        assert "version" in text

    def test_announce_on_request_includes_running_phrase(self):
        fake_tts = MagicMock()
        ann = VersionAnnouncer(tts=fake_tts)
        ann.announce_on_request()
        text = fake_tts.say.call_args[0][0].lower()
        assert "running" in text
        assert "version" in text

    def test_maybe_handle_responds_to_query(self):
        fake_tts = MagicMock()
        ann = VersionAnnouncer(tts=fake_tts)
        assert ann.maybe_handle("what version are you?") is True
        assert fake_tts.say.called

    def test_maybe_handle_ignores_other_utterances(self):
        fake_tts = MagicMock()
        ann = VersionAnnouncer(tts=fake_tts)
        assert ann.maybe_handle("set a timer") is False
        assert not fake_tts.say.called

    def test_routes_through_audio_output(self):
        fake_tts = MagicMock()
        fake_output = MagicMock()
        ann = VersionAnnouncer(tts=fake_tts, audio_output=fake_output)
        ann.speak_version()
        # The output object must be passed through to TTS.say()
        assert fake_tts.say.call_args.kwargs.get("output") is fake_output
