# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [0.8.2] - 2026-04-27

### Fixed
- systemd units (`desktop-assistant-core.service`,
  `desktop-assistant-thermal.service`) had `PrivateTmp=true`, which gave
  the services their own `/tmp` namespace and made the
  `ipc:///tmp/desktop-assistant.{pub,rep}` sockets invisible to the CLI
  (every command timed out). Now `PrivateTmp=false` with explicit
  `ReadWritePaths=` for the telemetry DB and `/sys/class/pwm`.
- `Environment=PYTHONPATH=...` value containing a space is now properly
  quoted so systemd no longer logs `Invalid environment assignment`.
- `StartLimitIntervalSec=` / `StartLimitBurst=` moved from `[Service]`
  to `[Unit]`, where systemd actually reads them.
- Removed empty placeholder `desktop-assistant.service` meta unit.

### Changed
- New `desktop-assistant-thermal.service` allows `ProtectHome=read-only`
  but no longer denies `/sys/class/pwm` writes (needed by the
  hardware-PWM backend).

---

## [0.8.1] - 2026-04-27

### Fixed
- `scripts/setup_pi.sh` now symlinks `desktop-assistant` into
  `/usr/local/bin` so the CLI is on `PATH` after a fresh install.
  Previously you had to run it by full path or symlink it manually.

---

## [0.8.0] - 2026-04-27

### Added — Pre-Phase-3 hardening
- `src/services/telemetry_service.py` — new `TelemetryService` persists
  `thermal.temp / fan / rpm`, `motion.position`, `audio.level` to a
  SQLite ring buffer at `~/.local/share/desktop-assistant/telemetry.db`
  (200k rows/topic cap; flushed every 5 s; `telemetry.flush` event
  published per flush). `recent(topic, limit)` and `row_count()`
  accessors for tooling.
- `IPCBridge` now answers a new `cmd: "status"` returning
  `{version, uptime_s, services, last (per-topic), endpoints}`. It
  also tracks `service.started`/`service.stopped` events.
- `desktop-assistant status` (and `--json`) — pretty health/telemetry
  dashboard. Exit code is non-zero if anything is red, so it doubles
  as a probe for monit/systemd healthchecks.
- **Boot self-test** in `src/assistant/runner.py` — three seconds after
  services start, checks every service, plus thermal/vision/audio
  errors, and TTS-announces "All systems nominal." or the failure list.
- `.github/workflows/ci.yml` — GitHub Actions workflow runs the test
  suite on Python 3.11/3.12/3.13 plus a non-blocking ruff lint pass.
  Hardware-only deps (lgpio, picamera2, etc.) stay un-imported on
  Ubuntu runners thanks to existing simulation fall-backs.

### Changed
- `src/assistant/core_main.py` now also runs `TelemetryService`.
- Architecture diagram updated for the new service, new topic, new CLI
  command, and the on-disk telemetry DB.

---

## [0.7.0] - 2026-04-27

### Added — Phase 2.5: hardware-PWM fan + tach
- `src/thermal/fan.py` rewritten with a sysfs hardware-PWM backend at
  25 kHz (Noctua spec) on `/sys/class/pwm/pwmchip0/pwm1`. Falls back
  automatically to the legacy lgpio software PWM (10 kHz) when sysfs is
  unavailable, so the code keeps working before the overlay is active.
- `src/thermal/fan_tach.py` — new `FanTach` driver on GPIO6. Counts
  open-collector tach pulses with an lgpio falling-edge callback and
  exposes `rpm` over a 1-second sliding window (Noctua: 2 ppr).
- New bus topic `thermal.rpm` published by `ThermalService`.
- `thermal.fan` payload now includes `backend` (`sysfs` / `lgpio` / `sim`).
- `config/thermal.yaml` capturing pin assignments and PWM parameters.
- `tests/test_fan_and_tach.py` — sysfs PWM tests via `tmp_path`,
  fallback path, and tach RPM math (5 tests).

### Changed
- `scripts/setup_pi.sh` now appends
  `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4` to
  `/boot/firmware/config.txt` (idempotent).
- `docs/architecture/architecture.dot` updated for the new tach driver,
  the new bus topic, and the new wiring on GPIO6. Diagram regenerated.
- `docs/PROJECT_PHASES.md` — Phase 2.5 marked ✅ pending physical
  verification on the Pi after reboot.

### Pin map (unchanged-ish)
- **Fan PWM out** — GPIO13 (physical pin 33). Same wire; backend swaps
  from lgpio software PWM to kernel hardware PWM via the pwm-2chan
  overlay after reboot.
- **Fan tach in** — GPIO6 (physical pin 31), 10 kΩ pull-up to 3.3 V.
- No conflict with I²C-1 (GPIO2/3), UART (14/15), SPI0 (7–11), or any
  other interface.

---

## [0.6.1] - 2026-04-27

### Added
- `docs/architecture/architecture.dot` — Graphviz source of truth for the
  system architecture diagram. Renders to `architecture.pdf`,
  `architecture.svg`, and `architecture.png`.
- `docs/architecture/build.sh` — one-shot regenerator for all three
  rendered formats. Requires `graphviz`.
- `docs/architecture/README.md` — narrative explanation of the diagram,
  process model, bus topics, and update rules.
- New agent imperative **#7 — Architecture Diagram** in
  `.github/copilot-instructions.md`. Mandates that any change to
  services, drivers, hardware, systemd units, cross-service bus topics,
  external interfaces, or process boundaries must be reflected in the
  diagram (both `.dot` source and rendered outputs) in the same commit.

---

## [0.6.0] - 2026-04-27

### Added — Phase 2 complete
- `src/services/vision_service.py` — `VisionService`. Owns the
  `Camera`, runs a 10 fps capture loop, publishes `vision.frame_ready`
  metadata, and exposes `latest_frame()` to in-process callers.
  Supports `vision.capture_still` requests over the bus.
- `src/services/audio_capture_service.py` — `AudioCaptureService`.
  Continuous mic capture in 250 ms chunks; publishes `audio.level`
  (dBFS + RMS) and `audio.chunk` metadata; `latest_chunk()` accessor.
- `src/services/ipc_bridge.py` — `IPCBridge`. ZeroMQ PUB on
  `ipc:///tmp/desktop-assistant.pub` forwards every bus event to
  external subscribers (two-frame: topic + JSON payload). REP on
  `ipc:///tmp/desktop-assistant.rep` accepts `publish` / `last` /
  `topics` / `ping` commands. pyzmq is a soft dependency — bridge
  cleanly disables itself if unavailable.
- `scripts/desktop-assistant` — CLI talking to the IPC bridge.
  Subcommands: `ping`, `topics`, `last <topic>`, `publish <topic>
  --payload <json>`, `pan --to <deg>`, `say <text>`, `version`,
  `watch [--topic <prefix>]` (live event stream).
- `tests/test_phase2_services.py` — 13 new tests covering vision,
  audio capture, and the full IPC round-trip (real ZMQ sockets over
  `tmp_path` IPC endpoints, no mocks).

### Changed
- `src/assistant/core_main.py` now starts five services in order:
  motion → vision → audio_capture → av → ipc_bridge. The bridge
  starts last so external subscribers see the `service.started`
  events of every other service.
- `scripts/setup_pi.sh` installs `python3-zmq`.

### Phase 2 exit-criteria status
- ✅ Services run under systemd (split: `desktop-assistant-thermal` +
  `desktop-assistant-core`)
- ✅ External IPC via ZeroMQ (PUB telemetry + REP control)
- ✅ Camera capture in `vision_service`
- ✅ Mic capture in `audio_capture_service`
- ✅ All five services publish/subscribe through the shared bus

### Tests
- **135 / 135** passing.

## [0.5.4] - 2026-04-27

### Added
- Phase 2.5 in `docs/PROJECT_PHASES.md`: migrate fan PWM from `lgpio`
  software PWM (10 kHz, faintly audible) to the kernel hardware-PWM
  driver so the NF-A6x25 runs silently at the Noctua-spec 25 kHz.
  Sits between Phase 2 (core services) and Phase 3 (perception).
  Tracked as todo `fan-hw-pwm-25khz`.

## [0.5.3] - 2026-04-27

### Fixed
- **Fan PWM 'bad PWM frequency' error.** `lgpio.tx_pwm()` only accepts
  software-PWM frequencies up to 10 kHz, but `src/thermal/fan.py` was
  configured for 25 kHz (Noctua spec). Lowered to 10 kHz so lgpio
  accepts it. Slight audible whine is the trade-off; for true 25 kHz
  silent operation we'll move to the kernel hardware-PWM driver later
  (`dtoverlay=pwm-2chan` + `/sys/class/pwm`). Hardware safety preserved
  — failsafe still drives 100%% duty on any error.
- **`ThermalService.run_tick()` TypeError: 'float' object is not callable.**
  `ThermalManager` exposes `temperature_c`, `fan_duty`, and `sensor_ok`
  as `@property`, but the service called them like methods. Now reads
  them as attributes. Updated the test fake to match the real API.

## [0.5.2] - 2026-04-27

### Fixed
- `scripts/test_camera.py --preview` aborted on QtGL with
  `RuntimeError: Format RGB888 not supported by QGlPicamera2 preview`.
  QtGL requires a 4-channel pixel format. Each preview backend now
  configures the camera with the format it actually supports:
  - QtGL → `XRGB8888`
  - Qt (software) → `RGB888`
  - DRM → `RGB888`
  Each backend is tried in turn with its own `Picamera2` instance, so
  a failure in one cleanly closes the camera before trying the next.

## [0.5.1] - 2026-04-27

### Changed — split into two systemd units (hybrid isolation)
- Replaced single `desktop-assistant.service` with two units:
  - `desktop-assistant-thermal.service` — TMP117 + fan only.
    `Restart=always`, no rate limit, no dependencies. If anything else
    on the device crashes, thermal monitoring keeps running.
  - `desktop-assistant-core.service` — motion + AV (and future
    perception/dialog). `Restart=on-failure`, rate-limited.
    `Wants=` and `After=` thermal so it boots in the right order.
- Added shared `src/assistant/runner.py` — common boot/shutdown/signal
  handling factored out of the entry points.
- Added `src/assistant/thermal_main.py` and `src/assistant/core_main.py`
  as the two process entry points. Each owns its own `MessageBus`;
  cross-process events will get a transport in Phase 3 if needed.
- Removed the old single-process `src/assistant/main.py`.
- Updated `services/systemd/README.md` with the new install/observe
  commands and rationale for the split.
- Added `tests/test_entry_points.py` (4 tests) covering runner
  start/stop/exit-code paths and entry-point importability.
- Total: **122 / 122** tests passing.

### Why hybrid (and not full per-class split)?
- Thermal is the only **safety-critical** loop. Splitting it gives the
  one isolation guarantee that actually matters: an AV/motion crash
  cannot disable thermal management.
- Motion + AV stay in the same process so they keep using the cheap
  in-process `MessageBus`. No IPC, no serialization, no broker.
- When (if) perception or dialog later prove they need their own
  failure domain, we'll split them — and at that point we'll add a
  ZeroMQ transport to `MessageBus` rather than rewriting it now.

## [0.5.0] - 2026-04-27

### Added — Phase 2 service layer (started)
- `src/core/bus.py` — `MessageBus`, an in-process thread-safe pub/sub.
  Supports per-topic subscribers, wildcard `*` subscribers, one-shot
  subscriptions, payload caching (`last(topic)`), and a process-wide
  `default_bus()` singleton. Subscriber exceptions are isolated.
- `src/core/service.py` — `Service` base class. Standard lifecycle
  (`on_start` → daemon thread running `run_tick` → `on_stop`), context-
  manager support, publishes `service.started` / `service.stopped`.
- `src/services/thermal_service.py` — wraps `ThermalManager` and
  publishes `thermal.temp`, `thermal.fan`, `thermal.critical` (edge-
  triggered), `thermal.error` on the bus.
- `src/services/motion_service.py` — wraps `ServoController`. Subscribes
  to `motion.pan_to`, `motion.relax`, `motion.stop`; publishes
  `motion.position` and `motion.moved` (with planned direction).
- `src/services/av_service.py` — wraps `AudioOutput`, `TextToSpeech`,
  and `VersionAnnouncer`. Subscribes to `av.say`, `av.beep`,
  `av.utterance`, `av.announce_version`. Speaks the version on startup
  (FR-VR1) and routes verbal version queries via `maybe_handle()`
  (FR-VR2).
- `src/assistant/main.py` — top-level boot entry point: starts all
  services on the shared bus, handles SIGINT/SIGTERM for graceful
  shutdown.
- `services/systemd/desktop-assistant.service` — systemd unit; restarts
  on failure with rate-limit guard, runs as `starter`, logs to journal.
- `services/systemd/README.md` — install/enable/observe instructions.

### Tests
- `tests/test_bus.py` — 12 tests covering subscribe, unsubscribe, wildcards,
  one-shot, last-payload, exception isolation, singleton.
- `tests/test_service_base.py` — 7 tests covering start/stop lifecycle,
  tick repetition, double-start safety, context manager, exception
  swallowing.
- `tests/test_services.py` — 13 tests for thermal/motion/AV services
  using `MagicMock` drivers. Verifies bus topic contracts and edge-
  triggered critical thermal events.
- Total: **118 / 118** tests passing.

### Changed
- `scripts/test_camera.py` — added `--preview [SECONDS]` flag for live
  video. Tries QtGL → Qt → DRM preview backends in order. Default
  preview duration 15 s, Ctrl-C to exit early. Also added `--null`
  (skip still capture) and `--index N` (slot select). Default mode
  (no flags) still captures one still to `/tmp/camera_test.jpg`.

## [0.4.0] - 2026-04-27

### Added — Phase 1 audio stack (complete)
- `src/audio/output.py` — `AudioOutput` driver; auto-locates the Sabrent USB
  adapter by name, plays numpy waveforms, generates beep/sweep tones; sim mode.
- `src/audio/input.py` — `AudioInput` driver; system-default mic by default,
  named-device override, blocking `record()`; sim mode.
- `src/audio/tts.py` — `TextToSpeech` wrapper around espeak-ng; renders to WAV
  and routes through `AudioOutput` so all speech goes out the Sabrent USB; sim
  mode if espeak-ng missing. Public API stable for future Piper/Mimic 3 swap.
- `src/audio/version_announcer.py` — fulfils **FR-VR1..VR4**:
  `announce_startup()`, `announce_on_request()`, `maybe_handle(utterance)`
  with regex-matched verbal version queries.
- `tests/test_audio_output.py`, `tests/test_audio_input.py`, `tests/test_tts.py`
  — 40 new unit tests, fully hardware-free via monkeypatched `sounddevice`.
- `scripts/test_speaker.py` — left/right/sweep speaker test through Sabrent.
- `scripts/test_microphone.py` — 5 s recording + dBFS level meter, saves WAV.
- `scripts/test_tts.py` — greeting + startup announcement + version query.
- `hardware/audio/audio_notes.md` — Sabrent specs, TRS speaker wiring, espeak-ng,
  spoken-version mapping to FR-VR1..VR4.

### Changed
- `scripts/setup_pi.sh`: added `espeak-ng` apt package; verification step now
  also checks sounddevice, espeak-ng, and (informationally) hailortcli; final
  command list includes all 8 bring-up scripts.

### Notes
- 86 / 86 tests passing. Phase 1 hardware bring-up complete.
  Hardware connection (speakers, mic) deferred to user — drivers and bring-up
  scripts ready to run as soon as the hardware is wired.

---

## [0.3.0] - 2026-04-26

### Added
- `src/perception/hailo_probe.py` — three-layer Hailo-8 readiness probe:
  PCIe presence (`lspci`), HailoRT CLI installed, firmware identify call.
  Returns `HailoStatus` dataclass; `fully_ready` and `degrade_reason()`
  helpers support the project's CPU-fallback safety imperative.
- `tests/test_hailo_probe.py` — 13 unit tests, fully hardware-free
  (subprocess runner is injectable; sample `hailortcli` outputs included).
- `scripts/test_hailo.py` — Pi bring-up probe; exits 0 if ready, 1 if
  degraded with actionable next steps.
- `hardware/perception/hailo8_notes.md` — AI HAT+ specs, install steps,
  PCIe enablement, troubleshooting, project imperative.

### Notes
- Total tests: 46 / 46 passing. Phase 1 perception groundwork complete.

---

## [0.2.10] - 2026-04-26

### Changed
- **Switched off the venv model.** The project now runs on **system Python 3**
  on the Pi. Hardware libs (picamera2, libcamera, lgpio) are apt-only on
  Pi 5 / Bookworm — running them through a venv added friction with no
  benefit on a dedicated appliance.
- `scripts/setup_pi.sh`: rewritten to install everything system-wide via
  apt + `pip3 --break-system-packages` for the few PyPI-only packages
  (smbus2, Adafruit-Blinka, adafruit-circuitpython-servokit). Final step
  verifies all key imports.
- `scripts/test_camera.py`: header and ImportError message reflect
  system-Python invocation (`python3 scripts/test_camera.py`).
- `hardware/vision/camera_notes.md`: bring-up command and known-issues
  updated for system Python.

---

## [0.2.9] - 2026-04-26

### Fixed
- `scripts/setup_pi.sh`: create venv with `--system-site-packages` so
  apt-installed `python3-picamera2` (and its libcamera bindings, which
  have no PyPI wheel) are visible to the project. **Root cause** of
  picamera2 ImportError in the venv.
- `requirements.txt`: removed `picamera2` (cannot be pip-installed on Pi 5);
  added clarifying comment.
- `scripts/test_camera.py`: ImportError message now points at the real fix
  (recreate venv with `--system-site-packages`).
- `hardware/vision/camera_notes.md`: Known Issues section updated.

---

## [0.2.8] - 2026-04-26

### Added
- `src/vision/camera.py` — `Camera` driver for Pi Camera Module 3 Wide (picamera2/libcamera); sim mode, context manager, `capture_frame()` / `capture_still()`
- `tests/test_camera.py` — 16 unit tests (all pass, hardware-free via Picamera2 stub)
- `scripts/test_camera.py` — Pi bring-up script; enumerates cameras, captures frame + JPEG still
- `hardware/vision/camera_notes.md` — IMX708 specs, wiring, FPC cable notes, known issues
- `requirements.txt`: added `picamera2>=0.3.12`
- `scripts/setup_pi.sh`: added `python3-picamera2` apt package + `test_camera.py` to test list

---

## [0.2.7] - 2026-04-25

### Fixed
- `scripts/setup_pi.sh`: force-reinstall lgpio via pip inside venv (apt version
  does not bind correctly on Pi 5 / Bookworm — root cause of fan driver failures)
- `hardware/thermal/TMP117_fan_notes.md`: documented lgpio venv reinstall requirement

### Notes
- Phase 1 hardware bring-up complete: TMP117 ✓, servo (channel 15) ✓, fan ✓

---

## [0.2.6] - 2026-04-25

### Fixed
- `servo_controller.py`: removed duplicate old-code block appended at line 192 (caused SyntaxError on import)
- `tests/test_servo_controller.py`: updated `TestLogicalToMechanical` assertions to match 0–180° kit-angle range (was 0–270° mechanical)

### Changed
- `requirements.txt`: added `Adafruit-Blinka>=9.0.0` and `adafruit-circuitpython-servokit>=1.3.0`
- All 17 servo unit tests pass

---

## [0.2.5] - 2026-04-25

### Added
- `scripts/debug_servo_raw.py` — raw PCA9685 register-level diagnostic tool.
  Bypasses all abstraction, prints every I²C write, sends centre/min/max/centre
  pulses directly to channel 15. Includes hardware checklist if servo still
  doesn't move.

---

## [0.2.4] - 2026-04-25

### Changed
- `src/core/pca9685.py` (**new**) — direct PCA9685 I²C driver via smbus2.
  Replaces Adafruit/Blinka dependency entirely. Handles prescaler, PWM
  on/off counts, and pulse-width-in-microseconds API.
- `src/motion/servo_controller.py` — rewired to use `src/core/pca9685.py`
  instead of `adafruit-circuitpython-servokit`. Added `mechanical_to_pulse_us()`
  static method and `close()`. `hardware_ready` now reflects PCA9685 init.
- `requirements.txt` — removed `adafruit-circuitpython-servokit` dependency.
- `scripts/test_servo.py` — updated prereq check to verify `smbus2` instead
  of adafruit-servokit.

---

## [0.2.3] - 2026-04-25

### Added
- `scripts/setup_pi.sh` — one-shot Pi dependency installer: system apt packages,
  I²C enable check, Python venv creation, all pip requirements including
  adafruit-circuitpython-servokit, and I²C device scan at the end.

---

## [0.2.2] - 2026-04-25

### Fixed
- `src/motion/servo_controller.py` — split catch-all `except Exception` into
  separate `ImportError` and `Exception` handlers so the reason for sim-mode
  fallback is clearly logged. Added `hardware_ready` property.
- `scripts/test_servo.py` — now **fails with exit code 1** if hardware is not
  initialised instead of silently passing in sim mode. Added prerequisite
  checker: I²C bus scan (via `i2cdetect`) and library import check, with
  actionable error messages pointing to the exact fix needed.

---

## [0.2.1] - 2026-04-25

### Changed
- `src/motion/servo_controller.py` — default `ServoConfig.channel` changed
  from 0 to **15** (pan servo is on PCA9685 channel 15).
- `hardware/servo/DS3218_notes.md` — noted channel 15 assignment and
  full 0–15 channel range of the SparkFun Pi Servo pHAT.

---

## [0.2.0] - 2026-04-25

### Added
- `requirements.txt` — Python dependencies (smbus2, adafruit-servokit, lgpio,
  gpiozero, sounddevice, numpy, pytest).
- `src/thermal/tmp117.py` — TMP117 I²C driver; register-level reads,
  device ID verification, °C and °F output.
- `src/thermal/fan.py` — NF-A6x25 PWM fan controller via lgpio hardware PWM
  (GPIO13 / 25 kHz); automatic fail-safe to 100% on error.
- `src/thermal/thermal_manager.py` — Background thermal control loop: polls
  TMP117, drives fan with proportional duty scaling, emits critical-temperature
  callback, fail-safes on sensor loss.
- `src/motion/servo_controller.py` — DS3218 pan servo controller via PCA9685
  (SparkFun Pi Servo pHAT). Enforces wrap-safe path planning: movements from
  a higher to a lower logical angle always traverse backward through the
  mechanical range, never crossing the 360°/1° dead zone.
- `tests/test_servo_controller.py` — 17 unit tests covering angle conversion,
  direction planning, and simulation-mode operation. All pass without hardware.
- `scripts/test_tmp117.py` — TMP117 bring-up script (5 samples, exits 0).
- `scripts/test_fan.py` — Fan ramp bring-up script (0–100%, exits 0).
- `scripts/test_servo.py` — Servo sweep + wrap-traversal bring-up script.
- `hardware/servo/DS3218_notes.md` — Wiring, pulse-width table, wrap rule.
- `hardware/thermal/TMP117_fan_notes.md` — TMP117 and NF-A6x25 wiring notes.

---

## [0.1.1] - 2026-04-25

### Changed
- Switched license from MIT to Apache 2.0 for patent protection.
- Updated copyright holder to Mark Mazurkiewicz.
- Updated README to reflect Apache 2.0 license.

---

## [0.1.0] - 2026-04-25

### Added
- Initial project scaffold: `src/`, `tests/`, `hardware/`, `services/`,
  `scripts/`, `config/` directory layout.
- `docs/PROJECT_PHASES.md` — printable 6-phase project plan.
- `docs/REQUIREMENTS.md` — hardware table, functional and non-functional requirements.
- `docs/VERSIONING.md` — versioning scheme and workflow rules.
- `README.md` — project overview and hardware list.
- `LICENSE` (MIT).
- `.gitignore` for Python projects.
- `.github/copilot-instructions.md` — agent imperatives (versioning, changelog,
  spoken version, hardware safety, commit hygiene).
- `VERSION` file — canonical version source of truth.
- `CHANGELOG.md` — this file.
- `src/core/version.py` — in-code version accessor with spoken-string helper.
