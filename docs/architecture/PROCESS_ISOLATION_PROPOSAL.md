---
title: Process Isolation Proposal — Fixing VERA's Monolithic Core
date: 2026-07-18
version: 1.47.0
status: Phase 1 (media) and Phase 2a (integrations) implemented and live — see §6 update below
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
own `desktop-assistant-integrations.service` process. See the "Phase 1" and
"Phase 2a" implementation-notes callouts in §6 for what actually shipped,
including two real bugs these splits surfaced and fixed.
`IoTService`/`SkillsService` (Phase 2b), `web` (Phase 3), and `audio-voice`
(Phase 4) remain unstarted.

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
| `integrations` *(Phase 2a — done: Telegram/Notification/Clock; Phase 2b — pending: IoT/Skills)* | `TelegramService`, `NotificationService`, `ClockService` *(done)*, `IoTService`, `NestService`, `YaleService`, `DropService`, `RadonService`, `SkillsService` *(pending — need `WebService` proxies first)* | Network/cloud-bound, low frequency, no hardware ownership; a crash or slow HTTP call here should never affect vision/audio |
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
4. **Phase 2b — `integrations` — IoT/Skills (not started):** Extract
   `IoTService` and `SkillsService`. Requires `media_client.py`-style
   `IPCClient` proxies for `WebService`'s `iot_registry` (10 call sites) and
   `skills_svc` (4 call sites) before the services themselves can move,
   mirroring how Phase 1 proxied `_music_svc`/`_podcast_svc`.
5. **Phase 3 — `web`:** Finish decoupling the remaining ~40 call sites
   (vision/tracking/skills/perception/depth), then extract. This is the
   highest-value split (isolates the 2,880-line file and its FastAPI/GIL
   load) and the highest-effort one — deliberately last.
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

- It moved three services out of `desktop-assistant-core` (Phase 1: media;
  Phase 2a: Telegram/Notification/Clock). `IoTService`/`SkillsService`
  (Phase 2b), `WebService` (Phase 3), and the vision/audio core group remain
  in `desktop-assistant-core` for now.
- It does **not** touch `scripts/desktop-assistant`'s existing
  `_request()`/`_thermal_request()` functions — refactoring the CLI to use
  `IPCClient` is a good follow-up but is out of scope here to keep this
  change purely additive.
- It does **not** adopt ROS2, for the reasons in §1.

## 9. Summary

VERA doesn't need a new framework — it needs to finish what the thermal
process already proved works. The scaffolding for that (`ProcessNode`,
`IPCClient`, and passing integration tests) is done, and two phases have now
shipped and are running live: Phase 1 (`media`) and Phase 2a
(`integrations` — Telegram/Notification/Clock). Both splits validated the
pattern end-to-end on real hardware and each surfaced (and fixed) a genuine
latent bug the monolith had been hiding. The next concrete step is Phase 2b
(`IoTService`/`SkillsService`), which needs `media_client.py`-style
`WebService` proxies before the services themselves can move — followed by
the higher-effort, higher-value Phase 3 (`web`).
