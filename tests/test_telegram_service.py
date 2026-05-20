"""Tests for TelegramService."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.services.telegram_service import TelegramService, _send_message


class _FakeBus:
    def __init__(self):
        self._subs: dict[str, list] = {}
        self.published: list[tuple] = []

    def subscribe(self, topic, cb):
        self._subs.setdefault(topic, []).append(cb)
        return lambda: self._subs[topic].remove(cb)

    def publish(self, topic, payload=None):
        self.published.append((topic, payload))
        for cb in self._subs.get(topic, []):
            cb(topic, payload)


# ---------------------------------------------------------------------------
# TelegramService disabled / missing credentials
# ---------------------------------------------------------------------------

class TestTelegramServiceDisabled:
    def test_disabled_does_not_subscribe(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=False, bot_token="tok", chat_id="123")
        svc.on_start()
        assert "face.greeted" not in bus._subs

    def test_missing_token_does_not_subscribe(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=True, bot_token="", chat_id="123")
        svc.on_start()
        assert "face.greeted" not in bus._subs

    def test_missing_chat_id_does_not_subscribe(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=True, bot_token="tok", chat_id="")
        svc.on_start()
        assert "face.greeted" not in bus._subs


# ---------------------------------------------------------------------------
# face.greeted → message dispatch
# ---------------------------------------------------------------------------

class TestFaceGreetedForwarding:
    def _make_svc(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=True, bot_token="TOKEN", chat_id="CHAT")
        svc.on_start()
        return svc, bus

    def test_new_face_dispatches_with_emoji(self):
        svc, bus = self._make_svc()
        sent: list[tuple] = []

        def mock_dispatch(text, chat_id=None):
            sent.append((text, chat_id))

        svc._dispatch = mock_dispatch
        bus.publish("face.greeted", {
            "face_id": "abc123", "name": "Alice",
            "text": "Oh hey, I don't think we've met!",
            "event_type": "new_face",
        })
        assert len(sent) == 1
        assert sent[0][0].startswith("👋")
        assert "Oh hey" in sent[0][0]

    def test_returning_face_dispatches_with_emoji(self):
        svc, bus = self._make_svc()
        sent: list[tuple] = []
        svc._dispatch = lambda t, chat_id=None: sent.append(t)
        bus.publish("face.greeted", {
            "face_id": "abc123", "name": "Bob",
            "text": "Hey Bob! Good to see you.",
            "event_type": "returning",
        })
        assert len(sent) == 1
        assert sent[0].startswith("👤")

    def test_named_face_dispatches_with_emoji(self):
        svc, bus = self._make_svc()
        sent: list[tuple] = []
        svc._dispatch = lambda t, chat_id=None: sent.append(t)
        bus.publish("face.greeted", {
            "face_id": "abc123", "name": "Carol",
            "text": "Nice to meet you, Carol! I'll remember you.",
            "event_type": "named",
        })
        assert len(sent) == 1
        assert sent[0].startswith("🏷️")

    def test_empty_text_not_dispatched(self):
        svc, bus = self._make_svc()
        sent: list[tuple] = []
        svc._dispatch = lambda t, chat_id=None: sent.append(t)
        bus.publish("face.greeted", {"face_id": "x", "name": "N", "text": "", "event_type": "returning"})
        assert len(sent) == 0


# ---------------------------------------------------------------------------
# telegram.send generic topic
# ---------------------------------------------------------------------------

class TestGenericSend:
    def test_telegram_send_dispatches(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=True, bot_token="T", chat_id="C")
        svc.on_start()
        sent: list[tuple] = []
        svc._dispatch = lambda t, chat_id=None: sent.append((t, chat_id))
        bus.publish("telegram.send", {"text": "Hello from a test"})
        assert len(sent) == 1
        assert sent[0][0] == "Hello from a test"

    def test_telegram_send_custom_chat_id(self):
        bus = _FakeBus()
        svc = TelegramService(bus=bus, enabled=True, bot_token="T", chat_id="C")
        svc.on_start()
        sent: list[tuple] = []
        svc._dispatch = lambda t, chat_id=None: sent.append((t, chat_id))
        bus.publish("telegram.send", {"text": "Hi", "chat_id": "OTHER"})
        assert sent[0][1] == "OTHER"


# ---------------------------------------------------------------------------
# on_stop cleans up subscriptions
# ---------------------------------------------------------------------------

def test_on_stop_unsubscribes():
    bus = _FakeBus()
    svc = TelegramService(bus=bus, enabled=True, bot_token="T", chat_id="C")
    svc.on_start()
    assert "face.greeted" in bus._subs
    svc.on_stop()
    assert len(bus._subs.get("face.greeted", [])) == 0
