# Desktop Assistant — Project Phases

> Printable plan. Each phase lists goal, deliverables, exit criteria.

---

## Phase 0 — Project Setup
**Goal:** Establish repo, docs, and dev environment.
**Deliverables:**
- Git repository on GitHub (public)
- README, REQUIREMENTS, PROJECT_PHASES docs
- Folder scaffold for hardware, src, services, tests
- Python virtualenv + `requirements.txt`
**Exit Criteria:** Clean clone-and-run baseline; CI lint passes.

---

## Phase 1 — Hardware Bring-Up
**Goal:** Validate every component in isolation.
**Deliverables / Smoke tests:**
- DS3218 servo via SparkFun Pi Servo pHAT — sweep test (1°–360° with wrap traversal)
- NF-A6x25 PWM fan — duty-cycle ramp test
- SparkFun Qwiic TMP117 — I²C read loop
- Stereo cameras — frame capture, sync check
- Dual microphones — record + level meter
- Sabrent USB audio — playback test
- Hailo-8 — `hailortcli` device probe
**Exit Criteria:** Each `scripts/test_<device>.py` exits 0.

---

## Phase 2 — Core Services
**Goal:** Long-running daemons that own each subsystem.
**Deliverables:**
- `thermal_service` — reads TMP117, drives fan PWM via control loop
- `motion_service` — exposes pan API; enforces 270° mechanical limits and
  long-way traversal across the 360°↔1° wrap
- `av_service` — camera + mic capture, audio out mux
**Exit Criteria:** Services run under systemd; expose IPC (DBus or ZeroMQ).

---

## Phase 3 — Perception
**Goal:** Real-time vision & audio understanding.
**Deliverables:**
- Person/face detection on Hailo-8
- Stereo depth → target localization
- VAD + wake-word + streaming STT
**Exit Criteria:** ≤150 ms latency face detect; reliable wake-word at 2 m.

---

## Phase 4 — Assistant Logic
**Goal:** Conversational behavior.
**Deliverables:**
- Intent router
- Dialog/state manager
- TTS pipeline → Sabrent audio out
**Exit Criteria:** End-to-end voice round-trip working.

---

## Phase 5 — Animation & Personality
**Goal:** "Semi-animated" presence.
**Deliverables:**
- Gaze tracking — pan servo follows detected face
- Idle micro-movements
- Audio-reactive head motion
**Exit Criteria:** Smooth tracking; respects servo wrap-traversal rule.

---

## Phase 6 — Integration & Packaging
**Goal:** Boot-to-assistant on power-on.
**Deliverables:**
- systemd unit files
- Config in `/etc/desktop-assistant/`
- Update / rollback script
- User docs
**Exit Criteria:** Cold-boot to ready state &lt; 60 s; survives 24 h soak.
