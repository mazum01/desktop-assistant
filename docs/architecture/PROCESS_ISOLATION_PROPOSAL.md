---
title: Process Isolation Proposal — Fixing VERA's Monolithic Core
date: 2026-07-18
version: 1.47.0
status: Phase 1 (media), Phase 2a (integrations: Telegram/Notification/Clock), Phase 2b (integrations: IoT/Skills), and Phase 3 (web) implemented AND deployed live (2026-08-01) — see §6 update below. All five processes (thermal/core/media/integrations/web) are running in production.
---

# Process Isolation Proposal

**Companion to:** `docs/ARCHITECTURE_REVIEW.md` §5.1 ("Monolithic single-process
core with 116 threads")
**Status:** Design + reusable scaffolding + integration tests are complete
and merged. **Phase 1 (`media`) has been implemented** — `MusicService` and
`PodcastService` now run in their own `desktop-assistant-media.service`
process, wired via the same `ProcessNode`/`IPCBridge` pattern this document
proposed. **Phase 2a (`integrations`) has also been implemented** —
`TelegramService`, `NotificationService`, and `ClockService` now run in their
own `desktop-assistant-integrations.service` process. **Phase 2b
(`integrations`) has also been implemented** — `IoTService` and
`SkillsService` now run in the same `desktop-assistant-integrations.service`
process alongside the Phase 2a trio, via new `IoTRegistryProxy`/
`SkillsServiceProxy` proxies. **Phase 3 (`web`) has also been implemented** —
`WebService` (the FastAPI dashboard/API) now has its own
`desktop-assistant-web.service` process entry point (`src/assistant/
web_main.py`), with all ~55 remaining direct-object call sites converted to
proxy calls (`src/core/web_client.py`) or `bus.last(...)` fallbacks. See the
"Phase 1", "Phase 2a", "Phase 2b", and "Phase 3" implementation-notes
callouts in §6 for what actually shipped, including real bugs/gaps these
splits surfaced and fixed. **Phase 3 is code-complete, unit- and
integration-tested (`tests/test_web_process_split.py`), the full suite
passes (993 tests), and it has been deployed live** (2026-08-01) as a
`--user` systemd unit (`~/.config/systemd/user/desktop-assistant-web.service`),
the same pattern already used for media/integrations on this box. The
rollout surfaced one real bug — `src/watchdog/watchdog.py`'s
`ManagedService` for `desktop-assistant-core.service` still pointed its
`http_check` at `localhost:8080/health`, which now belongs to `web`; this
made the watchdog's orphan-port-holder safety net kill the freshly
started `web` process within minutes, mistaking it for an orphan holding
core's port. Fixed by moving the health check to a new
`ManagedService(unit="desktop-assistant-web.service", user_unit=True, ...)`
entry (commit `1895c66`). All five processes (thermal/core/media/
integrations/web) are confirmed live and responding. `audio-voice`
(Phase 4) remains unstarted.

---

## 1. The Question: Should VERA Adopt ROS2?

The user asked directly whether something like ROS (i.e. ROS2, since ROS1 is
end-of-life) is the right fix. Short answer: **no** — not because ROS2 is
bad, but because it solves a different problem than the one VERA has, at a
much higher adoption cost.

### What ROS2 is actually for

ROS2 is built around DDS (Data Distribution Service): a discovery protocol,
QoS-negotiated topics, `.msg`/`.srv`/`.action` interface definitions compiled
per-language, a `colcon`/`ament` build/workspace system, `tf2` for coordinate
frame trees, and a large ecosystem (Nav2, MoveIt, rviz, rqt) aimed at
**multi-sensor motion planning, navigation, and manipulation** — the problems
a mobile robot arm or autonomous vehicle has.

### Why it's a poor fit here

| Concern | ROS2 | VERA's actual need |
|---|---|---|
| Problem shape | N sensors/actuators need synchronized motion planning across space and time | A fixed-position desk assistant needs to fan work out to a few OS processes on one board |
| Discovery | DDS multicast discovery (can be flaky on constrained/segmented networks, non-trivial resource cost) | Everything runs on **one Raspberry Pi 5** — no network discovery is needed at all |
| Interface definitions | Compiled `.msg`/`.srv` schemas, per-language codegen | Python-only, JSON payloads already flow through the existing bus |
| Migration cost | Full rewrite: every `Service` becomes a `Node`, every bus topic becomes a `.msg`, package.xml + colcon workspace restructuring | None of VERA's 31 services are ROS-node-shaped; this would be a near-total rewrite |
| Solves the Hailo contention problem? | **No** — ROS2 doesn't know or care about a shared PCIe accelerator; the single-VDevice-per-process constraint (see §3) exists regardless of message-passing framework | Must be solved by process topology, not by the bus technology |
| Already-proven alternative | N/A | VERA **already has** a working ZeroMQ pub/sub + RPC bridge, proven in production for the thermal↔core split |

**Recommendation: do not adopt ROS2.** Instead, generalize the pattern VERA
already has and has already proven in production.

---

## 2. The Existing, Proven Pattern

`src/assistant/thermal_main.py` already runs as a **fully independent OS
process** with its own `MessageBus`, and it already talks to `core` over
ZeroMQ via `src/services/ipc_bridge.py`:

```
thermal process                     core process
┌─────────────────┐                 ┌─────────────────────────┐
│ MessageBus       │  PUB  ────────► │ SUB (upstream_endpoints) │
│ ThermalService    │ ipc://…-thermal.pub │  forwards thermal.* onto │
│ IPCBridge (PUB/REP)│                 │  core's own MessageBus   │
└─────────────────┘  ◄──── REP ────  │ IPCBridge (PUB/REP)       │
                       (fan-control  └─────────────────────────┘
                        RPC calls)
```

This has been running in production for multiple releases (fan control, TMP117
readings, watchdog restarts, and `vera fan`/`vera fan-tach` CLI RPCs all flow
over this exact link right now). The wire format is two ZeroMQ patterns:

- **PUB/SUB** — every `bus.publish(topic, payload)` call is mirrored onto a
  ZeroMQ `PUB` socket as a two-frame `(topic, json)` message. Any other
  process can `SUB` to it and re-publish onto its own local bus
  (`IPCBridge(upstream_endpoints=[...])` already does this).
- **REQ/REP** — `IPCBridge.register_rpc(cmd, fn)` lets a process expose
  synchronous request/reply commands (`ping`, `status`, `fan_control_points`,
  etc.) that any other process can call with a plain REQ socket.

**This is structurally the same idea ROS2's topics + services provide** —
just without DDS discovery, without compiled schemas, and already written in
Python with zero new dependencies (`pyzmq`, already a soft dependency).

### What was missing

The pattern existed but was hand-wired once (for thermal) and copy-pasted
partially for the CLI (`scripts/desktop-assistant` has two nearly-identical
`_request()`/`_thermal_request()` functions). There was no reusable way to
spin up a *third*, *fourth*, *fifth* node without re-deriving all of this
wiring each time.

### What this proposal adds (implemented now)

Two new library modules, both additive — **nothing existing was changed**:

- **`src/core/process_node.py` — `ProcessNode`**
  Wraps `MessageBus` + `IPCBridge` + `run_services()` into one object.
  Creating a new isolated process is now:
  ```python
  node = ProcessNode(
      name="media",
      pub_endpoint="ipc:///tmp/desktop-assistant-media.pub",
      rep_endpoint="ipc:///tmp/desktop-assistant-media.rep",
      upstream_endpoints=["ipc:///tmp/desktop-assistant.pub"],
  )
  node.add_services(MusicService(bus=node.bus), PodcastService(bus=node.bus))
  raise SystemExit(node.run())
  ```
  This is literally what `thermal_main.py` does today, just generalized so
  the next split doesn't require re-reading and re-deriving that file.

- **`src/core/ipc_client.py` — `IPCClient`**
  Dedups the REQ-socket boilerplate currently duplicated in
  `scripts/desktop-assistant`. Any new node (or the CLI, eventually) gets a
  single `client.call({"cmd": "..."})` / `client.ping()` API instead of
  hand-rolling `zmq.REQ` + timeout/LINGER options each time.

- **`tests/test_process_node.py`** — binds two independent `ProcessNode`s to
  distinct `ipc://` socket paths (the same transport real separate OS
  processes use) and proves, with a real ZeroMQ round trip:
  1. an event published on node A is forwarded onto node B's bus
     (validates the exact mechanism that already carries `thermal.*`
     telemetry into `core`);
  2. an RPC call placed via `IPCClient` reaches a handler registered on the
     target node and returns its reply;
  3. calling a node that isn't running times out cleanly instead of hanging.

  All 4 tests pass today, proving the general pattern before any production
  service is actually moved.

---

## 3. Why This Matters: The Hailo Contention Constraint

This session's Hailo-Whisper benchmarking hit a hard wall: a second OS
process could not open the Hailo-8 `VDevice` while `desktop-assistant-core`
held it (`HAILO_OUT_OF_PHYSICAL_DEVICES`, status 74). `src/perception/hailo_inference.py`
already uses a **process-wide singleton** (`_shared_vdevice`) specifically so
multiple HEF models can multiplex within *one* process — but that singleton
cannot help across process boundaries.

**Implication for the process-split design:** every service that currently
calls into `hailo_inference.py` (face detection, object detection, mono
depth) **must stay in the same OS process** unless/until Hailo's
multi-process scheduling story changes. This is the one hard constraint the
grouping below is built around.

---

## 4. Proposed Target Topology

| Process | Services | Why grouped together |
|---|---|---|
| `thermal` *(exists today, unchanged)* | `ThermalService` | Safety-critical; must never share a process/GIL with anything that can hang |
| `watchdog` *(exists today, unchanged)* | External supervisor (`src/watchdog/watchdog.py`) | Already isolated; monitors the others via `systemctl` + HTTP, not the bus |
| `vision-hailo` | `VisionService`, `FaceService`, `ObjectService`, `PerceptionService`, `TrackingService`, `MonoDepthService`, `StereoService`, `DenseStereoService`, `PrivacyService`, `RoomService`, `MotionService` (servo) | **Must** share the Hailo `VDevice` singleton (§3); servo/tracking are latency-coupled to vision output, and `ipc://` (Unix domain socket) round trips are sub-millisecond, so keeping `MotionService` here vs. splitting it further costs nothing in practice |
| `audio-voice` | `AudioCaptureService`, `VoiceCommandService`, `AVService` (TTS/output) | Wake-word → STT → intent → TTS is the most latency-sensitive path in the system (NFR: ≤1.5s wake-to-response); keep it in one process to avoid adding any IPC hop to the critical path |
| `media` *(Phase 1 — done)* | `MusicService`, `PodcastService` | Loosely coupled, no cross-service object references, no hardware contention — **lowest-risk first extraction candidate** |
| `integrations` *(Phase 2a — done: Telegram/Notification/Clock; Phase 2b — done: IoT/Skills)* | `TelegramService`, `NotificationService`, `ClockService`, `IoTService`, `NestService`, `YaleService`, `DropService`, `RadonService`, `SkillsService` *(all done)* | Network/cloud-bound, low frequency, no hardware ownership; a crash or slow HTTP call here should never affect vision/audio |
| `web` | `WebService` (dashboard/API/websocket) | Isolates the single largest, most complex file (2,880 lines) so a slow request handler no longer shares a GIL with motion/vision/audio |

Every process links to a common backbone the same way `core` already links
to `thermal` today: each new `ProcessNode` declares
`upstream_endpoints=["ipc:///tmp/desktop-assistant.pub"]` (or whichever
processes it needs telemetry from), and `core`'s own bridge adds the new
process's PUB endpoint as one more upstream so the CLI/dashboard keep seeing
a unified event stream.

---

## 5. The Real Blocker: `WebService`'s Direct Object Coupling

Before `web` (or any group) can actually move to its own process, its
service code must stop holding **direct Python object references** to
services outside its own process — those references simply won't exist
across a process boundary.

Investigation this session found `WebService.__init__` accepts **14 other
service objects directly** (`vision_service`, `motion_service`,
`tracking_service`, `music_service`, `podcast_service`, `camera2_service`,
`object_service`, `skills_service`, `perception_service`,
`dense_stereo_service`, `mono_depth_service`, `room_service`,
`privacy_service`, `iot_registry`), called at **68 call sites** across the
file:

| Service reference | Call sites |
|---|---|
| `_music_svc` | 14 |
| `_podcast_svc` | 13 |
| `_iot_registry` | 10 |
| `_tracking_svc` | 5 |
| `_camera2_svc` | 5 |
| `_vision_svc` | 4 |
| `_skills_svc` | 4 |
| `_room_svc` | 4 |
| `_motion_svc` | 3 |
| `_dense_stereo_svc` | 2 |
| `_mono_depth_svc` | 2 |
| `_object_svc` | 1 |
| `_perception_svc` | 1 |
| `_privacy_svc` | 0 |

This is a real, non-trivial refactor — each call site needs to become either
a bus-published state snapshot the web process subscribes to (for read-only
dashboard data) or an RPC call via `IPCClient`/`register_rpc` (for actions
like "skip track" or "unlock door"). It is exactly the kind of work the new
`IPCClient`/`ProcessNode` scaffolding exists to support, but it must be done
route-by-route, with tests, before `web` can be extracted. **This is why
`media` (zero cross-service object coupling, per `core_main.py`) is the
recommended first real extraction, not `web`, despite `web` being the
biggest win on paper.**

> **Update:** `_music_svc`/`_podcast_svc` (Phase 1) and `_iot_registry`/
> `_skills_svc` (Phase 2b) are now proxy objects (`MusicServiceProxy`/
> `PodcastServiceProxy`/`IoTRegistryProxy`/`SkillsServiceProxy`) — those four
> rows are resolved; the underlying services run in other processes.
> **Phase 3 update:** `_tracking_svc`, `_camera2_svc`, `_vision_svc`,
> `_room_svc`, `_motion_svc`, `_object_svc`, `_perception_svc`, and
> `_privacy_svc` are now also proxy objects (`src/core/web_client.py`),
> plus a new `_face_svc` proxy the cataloging pass found (the Anthropic
> toggle's fallback path, missed in the original count above).
> `_dense_stereo_svc`/`_mono_depth_svc` needed **no proxy at all**: every
> call site already had a `bus.last(...)` fallback for when the direct
> reference was `None`, and since both services publish their full payload
> on the bus on every update, that fallback is always in sync — the
> direct-object short-circuit was simply deleted. `_registry`
> (`FaceRegistry`) also needed no proxy: `PerceptionService`/`FaceService`
> already each open an independent `FaceRegistry` SQLite connection in the
> same process today, so `WebService` opening a third connection from a
> different OS process is the same pattern it already relied on, not a new
> risk. **All 14 original blockers are now resolved.**

---

## 6. Phased Rollout Plan

1. **Phase 0 (done):** `ProcessNode` + `IPCClient` scaffolding, integration
   tests. No behavior change to the running system.
2. **Phase 1 — `media` (done):** Extract `MusicService` + `PodcastService` into
   `src/assistant/media_main.py` + `desktop-assistant-media.service`. These
   have no cross-service object coupling today, so this is close to a pure
   "wrap in ProcessNode" change. Update `WebService`'s 27 `_music_svc`/
   `_podcast_svc` call sites to go through `IPCClient` RPCs registered by
   the new media node. This phase **validates the full pattern end-to-end
   on live hardware** (systemd unit, `Restart=always`, CLI/dashboard talking
   across the new boundary) before touching anything higher-stakes.

   > **Implementation notes (v1.46.0):** Shipped essentially as designed,
   > with three refinements discovered while building it:
   > 1. `WebService`'s 27 call sites did **not** need to be rewritten by
   >    hand. `src/core/media_client.py`'s `MusicServiceProxy`/
   >    `PodcastServiceProxy` duck-type the exact same public API
   >    (`state`, `current_song`, `set_volume()`, `search()`, …) and forward
   >    each call over `IPCClient` to the media node's RPC handlers.
   >    `WebService` holds these proxies exactly where it used to hold the
   >    real service objects — zero route-handler changes needed. Only one
   >    line changed in `web_service.py` (a private-attribute reach-in,
   >    `_eq_preset = "custom"`, became a new public `MusicService.
   >    mark_eq_custom()` method, since a private attribute can't be set
   >    across a process boundary).
   > 2. Commands stay one-way as designed (skills/CLI publish `music.*`/
   >    `podcast.*` onto core's bus → forwarded upstream into media, same
   >    mechanism thermal already used for telemetry — just running in the
   >    opposite direction). No CLI changes were needed at all: it already
   >    talked to `music.*`/`podcast.*` through the generic IPCBridge
   >    `publish`/`last` commands or the HTTP API, both of which keep
   >    working unmodified.
   > 3. **`PodcastService` had never actually been wired into
   >    `desktop-assistant-core.service`** (`web_service.py` accepted a
   >    `podcast_service` constructor arg that `core_main.py` never passed —
   >    so every `/api/podcasts/*` route silently 503'd in production). This
   >    split is the first time podcast search/subscribe/playback
   >    genuinely works end-to-end. It also surfaced a real bug: `PodcastService`
   >    defined a business method named `stop()` (stop playback) that had
   >    the exact same name as the inherited `Service.stop()` lifecycle
   >    method the runner calls on shutdown — silently shadowing it, so
   >    `on_stop()`, the thread join, and the `service.stopped` event never
   >    ran. Renamed to `stop_playback()`; added a regression test.
3. **Phase 2a — `integrations` — Telegram/Notification/Clock (done):** Extract
   the three zero-object-coupling services (`TelegramService`,
   `NotificationService`, `ClockService`) into `src/assistant/
   integrations_main.py` + `desktop-assistant-integrations.service`. `IoTService`
   and `SkillsService` were deliberately deferred to Phase 2b (see below) once
   auditing `WebService` showed they have real object coupling requiring
   proxies, unlike the other three.

   > **Implementation notes (v1.47.0):**
   > 1. **Scoping decision:** an audit of `WebService`'s direct object
   >    references to each of the 9 originally-proposed `integrations`
   >    services found `TelegramService`/`NotificationService`/`ClockService`
   >    have **zero** direct coupling (bus-only, exactly like `media`'s
   >    starting shape), while `IoTService` (`iot_registry`, 10 call sites)
   >    and `SkillsService` (`skills_svc`, 4 call sites) need
   >    `media_client.py`-style proxies first. Splitting the low-risk three
   >    now mirrors the Phase 1 playbook (validate the pattern before the
   >    harder cases) rather than blocking on proxy work.
   >    `NestService`/`YaleService`/`DropService`/`RadonService` were never
   >    directly instantiated in `core_main.py` — they're constructed lazily
   >    inside `IoTService`'s device-plugin wrappers, so they'll move
   >    automatically whenever `IoTService` does in Phase 2b.
   > 2. **IPCBridge forwarding is not transitive:** if process A subscribes
   >    upstream to B, and B relays an event from ITS OWN upstream C, B does
   >    not re-forward that C-originated event onward to A — each process
   >    that needs another's telemetry must subscribe **directly** to that
   >    process's PUB. `NotificationService` needs `thermal.temp`, so
   >    `integrations_main.py` subscribes to both core's AND thermal's PUB
   >    directly, exactly mirroring how `core_main.py` subscribes directly to
   >    thermal/media/integrations rather than relying on any one process as
   >    a relay.
   > 3. **`QuietHours` can't cross a process boundary by reference.** The new
   >    process keeps its own independent `QuietHours` instance, kept in sync
   >    via the existing `settings.quiet_hours_updated` bus event (already
   >    published by `WebService`/`QuietHoursSkill`, both still in core) —
   >    no new plumbing needed.
   > 4. **Found and fixed a real latent bug in `IPCBridge`'s anti-echo-loop
   >    guard**, exposed by this phase because `ClockService`/
   >    `NotificationService` are the first services to synchronously publish
   >    a *new* topic while handling an upstream-injected one (e.g. reacting
   >    to an injected `av.tell_joke` by publishing `av.say`). The guard used
   >    a boolean thread-local flag that stayed `True` for the entire nested
   >    call stack of the injected `bus.publish()`, so it incorrectly
   >    suppressed that *different* new topic from ever reaching the
   >    process's own PUB — meaning jokes/thermal alerts were spoken inside
   >    `integrations` but never actually reached `AVService` in core. Fixed
   >    by storing the specific injected *topic string* instead of a bare
   >    boolean, so only an exact echo of the same topic is suppressed. This
   >    fix benefits every process using `IPCBridge`, not just `integrations`.
   >    Verified live: `da status` now shows the correct "last spoken" text
   >    after a `tell me a joke` round-trip through `integrations` and back.
4. **Phase 2b — `integrations` — IoT/Skills (done):** Extract
   `IoTService` and `SkillsService` into the existing `integrations_main.py`
   process alongside the Phase 2a trio.

   > **Implementation notes:**
   > 1. **Proxy shape differs from Phase 1.** `MusicServiceProxy`/
   >    `PodcastServiceProxy` (Phase 1) mirror the original objects' public
   >    API 1:1 via duck-typing, because `WebService`'s coupling there was a
   >    handful of read-only properties. `WebService`'s IoT/Skills coupling
   >    is deeper — multi-step "get an object, then call methods on it" code
   >    (`registry.get(id)` then `dev.execute_action(...)`), and
   >    `api_iot_add` used to construct the device object itself using
   >    `bus=self.bus`. None of that can cross a process boundary by
   >    duck-typing alone, so `IoTRegistryProxy`/`SkillsServiceProxy`
   >    (`src/core/integrations_client.py`) instead expose **one method per
   >    `WebService` HTTP route** (`add`, `get_detail`, `update_config`,
   >    `delete`, `announce`, `execute_action`, …), and the corresponding RPC
   >    handler in `integrations_main.py` does the actual object
   >    manipulation locally, where the registry and its devices actually
   >    live. This required rewriting ~11 `web_service.py` route bodies
   >    (all IoT + skills routes) instead of the "zero route-handler
   >    changes" Phase 1 achieved — a real but mechanical, well-contained
   >    cost, paid once. `/api/status`'s `iot` field and `/api/iot/types`
   >    needed **no** change: `get_all_snapshots()` matches the proxy's
   >    method name, and `iot_loader.get_type_list()` is a pure function
   >    with no registry/bus dependency.
   > 2. **Reply contract:** every RPC reply distinguishes transport failure
   >    (`{"ok": False, "error": ...}`, generated by `IPCClient`/`IPCBridge`
   >    itself on timeout) from business-level not-found/bad-request
   >    (`{"ok": False, "reason": "not_found"|"bad_request", "error": ...}`)
   >    from success (`{"ok": True, ...fields}`), so `web_service.py` can map
   >    each case to the exact same HTTP status codes (404/400/503) it used
   >    before the split.
   > 3. **Found a real latent gap this split would have introduced:**
   >    `MusicControlSkill`/`VolumeSkill` publish `music.*` commands, and
   >    `SkillsService` now runs in `integrations`, not core — so those
   >    commands originate on *integrations'* bus, not core's. Per the
   >    "IPCBridge forwarding is not transitive" rule (§ Phase 2a notes),
   >    `media_main.py`'s existing `upstream_endpoints=[_CORE_PUB]` would
   >    have silently stopped receiving voice-driven music commands the
   >    moment `SkillsService` moved. Fixed by adding `_INTEGRATIONS_PUB` to
   >    `media_main.py`'s default upstream endpoints, mirroring how
   >    `integrations_main.py` itself subscribes to both core's and
   >    thermal's PUB. Caught by design review before deploy, not in
   >    production — but exactly the kind of gap this checklist exists to
   >    catch.
   > 4. **No new proxy needed for `av.say`/`av.utterance`.** `SkillsService`
   >    dispatching a skill that publishes `av.say` (still consumed by
   >    `AVService` in core) works automatically via the existing
   >    `_INTEGRATIONS_PUB` upstream link core already had from Phase 2a; the
   >    STT pipeline/CLI/`WebService`'s `/api/utterance` publishing
   >    `av.utterance` (still in core) reaches `SkillsService` via
   >    `integrations_main.py`'s existing `_CORE_PUB` upstream subscription.
   >    Neither direction needed new wiring — only the two proxies above and
   >    the `media_main.py` fix in note 3.
5. **Phase 3 — `web` (done, deployed live 2026-08-01):** Decoupled the
   remaining ~55 direct-object call sites (vision/tracking/motion/room/face/
   privacy/object/perception/camera2/depth) and extracted `WebService` into
   its own `src/assistant/web_main.py` process
   (`desktop-assistant-web.service`). This is the highest-value split
   (isolates the 2,900-line file and its FastAPI/GIL load) and was the
   highest-effort one — deliberately last.

   > **Implementation notes:**
   > 1. **Direction reverses vs. Phase 1/2b.** In Phase 1/2b, a satellite
   >    service moved *out* of core and core grew a client proxy pointed
   >    *at* it. Here, `WebService` itself is the thing that moves out, so
   >    `web_main.py` builds proxies (`src/core/web_client.py`) pointed
   >    *at* core's own default IPCBridge REP endpoint
   >    (`ipc:///tmp/desktop-assistant.rep`, already existed, reused as-is),
   >    and `core_main.py` registers ~15 new `register_rpc(...)` handlers
   >    against the real service objects, which stay in `core`.
   > 2. **Most of the remaining coupling turned out to be read-mostly.**
   >    Every write/toggle route (`/api/settings/servo`, `/api/pan`,
   >    `/api/settings/anthropic` PUT, etc.) already only called
   >    `self.bus.publish(...)`, which crosses the process boundary for
   >    free via the existing IPCBridge upstream mechanism — no proxy
   >    needed for those. Only the paired GET/read routes and two POST
   >    actions (`/api/faces/{id}/train`, snapshot JPEG encode) needed a
   >    proxy method.
   > 3. **Two services needed zero proxy work.** `DenseStereoService`/
   >    `MonoDepthService` publish their full payload on the bus on every
   >    update, and every `web_service.py` call site reading
   >    `.latest_payload()` already had a `bus.last(...)` fallback for when
   >    the direct reference was `None` — so the direct-object
   >    short-circuit was simply deleted, relying purely on the
   >    always-in-sync bus cache. `FaceRegistry` (`self._registry`) also
   >    needed no proxy: `PerceptionService`/`FaceService` already each open
   >    an independent SQLite connection to the same shared
   >    `~/.local/share/desktop-assistant/faces.db` in the same process
   >    today — `WebService` opening a third connection from a different OS
   >    process extends an already-proven pattern, not a new risk.
   > 4. **Camera2 "configured" gating changed shape.** The original code
   >    checked `self._camera2_svc is not None` as a plain truthy check
   >    (startup subscribe gate, several GET routes). Since a proxy object
   >    is never `None`, this became `Camera2ServiceProxy.is_configured()`,
   >    which caches its answer after the first successful RPC round trip
   >    (cam2's presence never changes at runtime) to avoid re-hitting the
   >    wire on every truthiness check — but deliberately does **not** cache
   >    a transport *failure*, so a later successful call (once `core` comes
   >    back up) is retried instead of permanently reporting cam2 as absent.
   > 5. **MJPEG streaming and snapshot JPEGs reuse the existing RPC
   >    mechanism**, not a new binary channel — bandwidth-calculated as
   >    trivial (~15–30KB base64 JPEG at cam1's ~11fps over local IPC).
   >    Snapshot routes now JPEG-encode server-side in `core`
   >    (`vision.snapshot_jpeg`/`camera2.snapshot_jpeg`) and return
   >    base64-encoded bytes, so raw ndarrays are never sent across the
   >    process boundary.
   > 6. **Reverse cross-wiring requirement, unique to this phase.** Unlike
   >    Phase 1/2b (satellite → core only), `web` both calls *into* core
   >    (RPCs above) and is itself an upstream *other* processes must
   >    subscribe to: `WebService`'s own `bus.publish()` calls (`music.*`,
   >    `av.utterance`, `settings.quiet_hours_updated`, `motion.pan_to`,
   >    etc.) now originate on `web`'s bus, not core's. Per the "IPCBridge
   >    forwarding is not transitive" rule (§ Phase 2a notes), `core_main.py`
   >    (its own `IPCBridge`), `media_main.py`, and `integrations_main.py`
   >    each needed a new `_WEB_PUB` added to their `upstream_endpoints` —
   >    symmetric to how `_INTEGRATIONS_PUB` was added to `media_main.py` in
   >    Phase 2b. `web_main.py` itself subscribes to **all four** other
   >    processes' PUBs (core, thermal, media, integrations) — the same
   >    breadth core itself has — not just core's, since `WebService` used
   >    to share core's in-process bus and therefore saw everything core
   >    itself received from anywhere.
   > 7. **Found one previously-uncataloged direct reference:** `_face_svc`
   >    (the Anthropic-toggle GET route's fallback when `_room_svc` is
   >    unavailable) — missed in the original §5 count, added as
   >    `FaceServiceProxy` alongside the rest.
   > 8. **Deployed live 2026-08-01** as a `--user` unit (same pattern as
   >    media/integrations — this box's passwordless sudo doesn't cover
   >    `daemon-reload`/`enable` for new system-level units). `media_main.py`/
   >    `integrations_main.py` also needed a `systemctl --user restart` to
   >    pick up the `_WEB_PUB` wiring code (they were still running
   >    processes from before that change landed).
   > 9. **Rollout bug found and fixed: watchdog killed the new `web`
   >    process.** `src/watchdog/watchdog.py`'s `ManagedService` for
   >    `desktop-assistant-core.service` still had
   >    `http_check="http://localhost:8080/health"` — a leftover from when
   >    core hosted the dashboard. Port 8080 (and `/health`) now belongs to
   >    `web`. The watchdog's `_kill_orphan_port_holder()` safety net (runs
   >    before a restart; resolves the port from `http_check` and kills
   >    whatever holds it if that PID isn't the unit's own systemd MainPID)
   >    found `web`'s PID instead of `core`'s, concluded `web` was an orphan
   >    squatting on core's port, and SIGTERM'd it within minutes of the live
   >    rollout — also triggering an unneeded `core` restart. Fixed by
   >    removing `http_check` from the `core` entry and adding a new
   >    `ManagedService(unit="desktop-assistant-web.service", user_unit=True,
   >    http_check="http://localhost:8080/health")` instead (commit
   >    `1895c66`). The watchdog process itself still needs its own restart
   >    to load this fix — not in the passwordless sudo list, so it requires
   >    a human with a password (`sudo systemctl restart
   >    desktop-assistant-watchdog.service`).
6. **Phase 4 (optional, higher risk) — `audio-voice` split from
   `vision-hailo`:** Only worth doing if profiling after phases 1–3 shows
   audio/voice latency is still affected by vision/Hailo load. Requires
   re-validating the ≤1.5s wake-to-response NFR after the split, since this
   is the one boundary where an added IPC hop touches the most
   latency-sensitive path in the system.
7. **`vision-hailo` stays a single process indefinitely**, per the Hailo
   VDevice constraint in §3, unless Hailo's own multi-process device sharing
   story changes upstream.

Each phase is independently revertable: the old code path isn't deleted
until the new one has run clean through a 24h soak (matching the existing
NFR), and each new unit gets `Restart=always` exactly like `thermal` and
`watchdog` do today.

---

## 7. Message & RPC Conventions (Recommended, Not Yet Enforced)

To keep the growing number of cross-process links maintainable:

- **Topic naming** stays as-is (`<domain>.<event>`, e.g. `thermal.temp`,
  `vision.frame_ready`) — already consistent across the codebase.
- **RPC command names** should be namespaced per node once there are
  several nodes (e.g. `media.skip`, `web.reload_config`) to avoid collisions
  now that `register_rpc` is used by more than one process.
- Consider a lightweight payload validation helper (a `TypedDict` or small
  `dataclass` per topic, validated at publish time in debug/test builds
  only) — this is a much smaller lift than adopting ROS2's `.msg` compiler
  and would catch payload-shape drift between processes early.

---

## 8. What This Proposal Does NOT Do

- It moved six services/service-groups out of `desktop-assistant-core`
  (Phase 1: media; Phase 2a: Telegram/Notification/Clock; Phase 2b:
  IoT/Skills; Phase 3: `WebService`). The vision/audio core group remains in
  `desktop-assistant-core` for now.
- It does **not** touch `scripts/desktop-assistant`'s existing
  `_request()`/`_thermal_request()` functions — refactoring the CLI to use
  `IPCClient` is a good follow-up but is out of scope here to keep this
  change purely additive.
- It does **not** adopt ROS2, for the reasons in §1.

## 9. Summary

VERA doesn't need a new framework — it needs to finish what the thermal
process already proved works. The scaffolding for that (`ProcessNode`,
`IPCClient`, and passing integration tests) is done, and four phases have
now shipped and are all live in production: Phase 1 (`media`), Phase 2a
(`integrations` — Telegram/Notification/Clock), Phase 2b (`integrations` —
IoT/Skills), and Phase 3 (`web` — the FastAPI dashboard/API, deployed
2026-08-01). All four splits validated the pattern end-to-end (real
hardware plus, for Phase 3, the full test suite and a dedicated
`tests/test_web_process_split.py` integration suite) and each surfaced
(and fixed) at least one genuine latent bug or gap the monolith had been
hiding — Phase 3's rollout itself surfaced one more: a stale watchdog
health-check mapping that briefly killed the newly-deployed `web` process
(see §6 note 9), fixed same-day. The only phase left is the optional,
higher-risk Phase 4 (`audio-voice` split from `vision-hailo`), worth doing
only if profiling shows it's still needed now that Phase 3 is live.
