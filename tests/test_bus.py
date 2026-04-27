"""Tests for src.core.bus.MessageBus."""

import pytest

from src.core.bus import MessageBus, default_bus


@pytest.fixture
def bus():
    return MessageBus()


def test_subscribe_and_publish_invokes_callback(bus):
    received = []
    bus.subscribe("foo", lambda t, p: received.append((t, p)))
    bus.publish("foo", {"x": 1})
    assert received == [("foo", {"x": 1})]


def test_unrelated_topics_dont_cross_fire(bus):
    received = []
    bus.subscribe("foo", lambda t, p: received.append(t))
    bus.publish("bar", "ignore me")
    assert received == []


def test_multiple_subscribers_all_get_called(bus):
    a, b = [], []
    bus.subscribe("topic", lambda t, p: a.append(p))
    bus.subscribe("topic", lambda t, p: b.append(p))
    bus.publish("topic", "hello")
    assert a == ["hello"]
    assert b == ["hello"]


def test_unsubscribe_stops_delivery(bus):
    received = []
    unsub = bus.subscribe("topic", lambda t, p: received.append(p))
    bus.publish("topic", 1)
    unsub()
    bus.publish("topic", 2)
    assert received == [1]


def test_subscribe_once_fires_only_once(bus):
    received = []
    bus.subscribe_once("topic", lambda t, p: received.append(p))
    bus.publish("topic", "a")
    bus.publish("topic", "b")
    assert received == ["a"]


def test_wildcard_subscriber_gets_all_topics(bus):
    seen = []
    bus.subscribe("*", lambda t, p: seen.append(t))
    bus.publish("foo", 1)
    bus.publish("bar", 2)
    assert seen == ["foo", "bar"]


def test_callback_exception_does_not_break_other_subscribers(bus):
    seen = []

    def boom(t, p):
        raise RuntimeError("kaboom")

    bus.subscribe("topic", boom)
    bus.subscribe("topic", lambda t, p: seen.append(p))
    bus.publish("topic", "ok")
    assert seen == ["ok"]


def test_last_returns_most_recent_payload(bus):
    bus.publish("topic", 1)
    bus.publish("topic", 2)
    assert bus.last("topic") == 2


def test_last_default_when_no_publish(bus):
    assert bus.last("nope", default="x") == "x"


def test_subscriber_count(bus):
    bus.subscribe("topic", lambda t, p: None)
    bus.subscribe("topic", lambda t, p: None)
    assert bus.subscriber_count("topic") == 2


def test_clear_resets_state(bus):
    bus.subscribe("topic", lambda t, p: None)
    bus.publish("topic", 1)
    bus.clear()
    assert bus.subscriber_count("topic") == 0
    assert bus.last("topic") is None


def test_default_bus_is_singleton():
    assert default_bus() is default_bus()
