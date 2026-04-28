"""Tests for TelemetryService."""

from __future__ import annotations

import time

from src.core.bus import MessageBus
from src.services.telemetry_service import TelemetryService


def test_telemetry_records_subscribed_topics(tmp_path):
    bus = MessageBus()
    svc = TelemetryService(
        bus=bus,
        db_path=tmp_path / "t.db",
        topics=("thermal.temp", "motion.position"),
    )
    svc.start()
    try:
        bus.publish("thermal.temp", {"celsius": 42.0, "ok": True})
        bus.publish("motion.position", {"angle": 90.0})
        bus.publish("audio.level", {"dbfs": -30.0})  # not subscribed → ignored
        # Wait for at least one tick (5 s default is too long); call _flush directly.
        svc._flush()

        assert svc.row_count("thermal.temp")    == 1
        assert svc.row_count("motion.position") == 1
        assert svc.row_count("audio.level")     == 0
        assert svc.row_count() == 2

        rows = svc.recent("thermal.temp", limit=10)
        assert rows[0][1]["celsius"] == 42.0
    finally:
        svc.stop()


def test_telemetry_enforces_row_cap(tmp_path):
    bus = MessageBus()
    svc = TelemetryService(
        bus=bus,
        db_path=tmp_path / "t.db",
        topics=("x.y",),
        row_cap_per_topic=10,
    )
    svc.start()
    try:
        for i in range(50):
            bus.publish("x.y", {"i": i})
        svc._flush()
        n = svc.row_count("x.y")
        assert n <= 11   # cap is enforced (off-by-one is acceptable)
        # The most recent entry should still be there
        latest = svc.recent("x.y", limit=1)[0][1]
        assert latest["i"] == 49
    finally:
        svc.stop()


def test_telemetry_publishes_flush_events(tmp_path):
    bus = MessageBus()
    flushed = []
    bus.subscribe("telemetry.flush", lambda t, p: flushed.append(p))

    svc = TelemetryService(
        bus=bus, db_path=tmp_path / "t.db", topics=("a.b",),
    )
    svc.start()
    try:
        bus.publish("a.b", {"v": 1})
        bus.publish("a.b", {"v": 2})
        svc._flush()
        assert len(flushed) == 1
        assert flushed[0]["rows"] == 2
    finally:
        svc.stop()
