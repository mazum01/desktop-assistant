"""Integration tests for Phase 1 of the process-isolation proposal: the
`media` process (MusicService + PodcastService) running as a real,
independent `ProcessNode`, exercised end-to-end through the same
`MusicServiceProxy`/`PodcastServiceProxy` that `WebService` uses in
production once music/podcast move out of `desktop-assistant-core`.

See docs/architecture/PROCESS_ISOLATION_PROPOSAL.md §6 (Phase 1).
"""

import time
import uuid
from unittest.mock import MagicMock

import pytest

pytest.importorskip("zmq")

from src.assistant.media_main import build_node
from src.core.ipc_client import IPCClient
from src.core.media_client import MusicServiceProxy, PodcastServiceProxy
from src.core.process_node import ProcessNode


def _unique_endpoints(prefix: str):
    tag = uuid.uuid4().hex[:8]
    return (
        f"ipc:///tmp/test-{prefix}-{tag}.pub",
        f"ipc:///tmp/test-{prefix}-{tag}.rep",
    )


def _wait_until(predicate, timeout_s: float = 3.0, interval_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


@pytest.fixture
def media_node(tmp_path, monkeypatch):
    """A real media ProcessNode (music disabled, so no pianobar subprocess
    is spawned) plus a fake "core" node it upstream-subscribes to, mirroring
    production wiring (core <-> media bidirectional forwarding)."""
    # Isolate from any real pianobar config / persisted podcast subscriptions
    # on the dev/prod machine running this test suite.
    import src.services.music_service as _music_module
    import src.services.podcast_service as _podcast_module

    monkeypatch.setattr(_music_module, "_PIANOBAR_CONFIG", tmp_path / "pianobar-config")
    monkeypatch.setattr(_music_module, "_MUSIC_EQ_STATE_FILE", tmp_path / "music_eq_preset.txt")
    monkeypatch.setattr(_podcast_module, "_STATE_DIR", tmp_path)
    monkeypatch.setattr(_podcast_module, "_PODCASTS_STATE_FILE", tmp_path / "podcasts.json")

    core_pub, core_rep = _unique_endpoints("core")
    media_pub, media_rep = _unique_endpoints("media")

    core = ProcessNode(name="core", pub_endpoint=core_pub, rep_endpoint=core_rep)
    node = build_node(
        cfg={"music": {"enabled": False}, "podcast": {"enabled": True}},
        pub_endpoint=media_pub,
        rep_endpoint=media_rep,
        upstream_endpoints=[core_pub],
    )

    for svc in core.services:
        svc.start()
    for svc in node.services:
        svc.start()

    time.sleep(0.3)

    try:
        yield core, node, media_rep
    finally:
        for svc in reversed(node.services):
            svc.stop()
        for svc in reversed(core.services):
            svc.stop()


def test_music_get_state_rpc_round_trips_through_proxy(media_node):
    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = MusicServiceProxy(client)

    # Music is disabled in this test's config, so it reports safe defaults —
    # this proves the RPC path works end-to-end, not pianobar itself.
    assert proxy.state == "stopped"
    assert proxy.stations == []
    assert proxy.is_configured is False
    assert proxy.eq_preset == "flat"


def test_music_set_volume_rpc_updates_state(media_node, monkeypatch):
    # MusicService.volume/set_volume/set_muted shell out to the real `wpctl`
    # (PipeWire) binary, which isn't installed on CI runners. Since the media
    # ProcessNode's services run as in-process threads (see Service.start()),
    # patching the module-level subprocess calls here reaches the actual
    # service thread and lets this stay a hermetic unit test rather than
    # depending on real audio hardware.
    import src.services.music_service as _music_module

    state = {"volume": 0.0, "muted": False}

    def fake_check_output(cmd, text=True):
        assert cmd[:2] == ["wpctl", "get-volume"]
        line = f"Volume: {state['volume']:.2f}"
        if state["muted"]:
            line += " [MUTED]"
        return line

    def fake_run(cmd, check=False, **kwargs):
        if cmd[:2] == ["wpctl", "set-volume"]:
            state["volume"] = float(cmd[3].rstrip("%")) / 100.0
        elif cmd[:2] == ["wpctl", "set-mute"]:
            state["muted"] = cmd[3] == "1"
        return MagicMock(returncode=0)

    monkeypatch.setattr(_music_module.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(_music_module.subprocess, "run", fake_run)

    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = MusicServiceProxy(client)

    proxy.set_volume(42)
    assert proxy.volume == 42

    proxy.set_muted(True)
    assert proxy.muted is True


def test_music_eq_preset_rpcs(media_node):
    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = MusicServiceProxy(client)

    proxy.set_eq_preset("bass_boost")
    assert proxy.eq_preset == "bass_boost"

    proxy.mark_eq_custom()
    assert proxy.eq_preset == "custom"


def test_podcast_list_and_search_rpcs_via_proxy(media_node):
    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = PodcastServiceProxy(client)

    # No subscriptions yet — proves the RPC path (not network access).
    assert proxy.subscriptions == []

    status = proxy.status()
    assert status["state"] == "stopped"
    assert status["subscriptions"] == 0


def test_podcast_unsubscribe_unknown_id_is_a_noop_not_an_error(media_node):
    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = PodcastServiceProxy(client)

    result = proxy.unsubscribe("does-not-exist")
    assert result["ok"] is True
    assert result["deleted"] == 0


def test_podcast_episodes_for_unknown_id_raises(media_node):
    _core, _node, media_rep = media_node
    client = IPCClient(media_rep, timeout_ms=2000)
    proxy = PodcastServiceProxy(client)

    with pytest.raises(RuntimeError):
        proxy.episodes("does-not-exist")


def test_command_published_on_core_bus_reaches_media_service(media_node):
    """Proves the bidirectional forwarding: a command published onto
    "core"'s bus (exactly what skills/CLI do today via `bus.publish(...)`
    or the generic IPCBridge "publish" RPC) is forwarded upstream into the
    media process and reaches MusicService's normal bus.subscribe()
    handler — no code change needed inside MusicService itself."""
    core, node, _media_rep = media_node

    received = []
    # Subscribe directly on the media node's own bus to observe forwarding,
    # since music is disabled (no pianobar) in this test.
    node.bus.subscribe("music.set_volume", lambda _t, payload: received.append(payload))

    core.bus.publish("music.set_volume", {"level": 77})

    assert _wait_until(lambda: len(received) == 1)
    assert received[0] == {"level": 77}


def test_proxy_falls_back_gracefully_when_media_process_unreachable():
    """If the media process isn't running (e.g. mid-restart), read-only
    proxy properties should degrade to safe defaults instead of raising —
    matching the old "if not self._music_svc: <default>" behavior."""
    client = IPCClient("ipc:///tmp/test-nobody-home.rep", timeout_ms=200)
    music_proxy = MusicServiceProxy(client)
    podcast_proxy = PodcastServiceProxy(client)

    assert music_proxy.state == "stopped"
    assert music_proxy.stations == []
    assert music_proxy.volume == -1
    assert podcast_proxy.subscriptions == []
    assert podcast_proxy.status()["state"] == "stopped"
