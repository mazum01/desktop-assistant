# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

---

## [1.24.2] - 2026-05-25
### Changed
- `mic_harness.dot/pdf/png/svg` — page reformatted to US Letter portrait
  (612 x 792 pts) and grounding redesigned for actual accessible ground
  points: I²C-header GND (amp side) + TRS sleeve (receiver side), with
  copper-foil shield floating at the amp end and drained only at the TRS
  sleeve. No reliance on Pi GPIO pin 6.
- `mic_harness.md` — build guide updated to match the two-star grounding
  scheme and clarified the "no extra GND wire between the two stars" rule.

## [1.24.1] - 2026-05-25
### Added
- `hardware/audio/mic_harness.dot/.png/.svg/.pdf` — filtered & shielded mic
  wiring harness schematic: ferrite + 3-cap power filter on MAX4466 VDD,
  1 kΩ + 4.7 nF RF low-pass on OUT, twisted-pair with single-end-grounded
  copper foil shield.
- `hardware/audio/mic_harness.md` — full build guide with BOM, step-by-step
  assembly, common mistakes, and verification procedure.

## [1.24.0] - 2026-05-25
### Added
- `security-scan` skill: sub-agent that audits the codebase for privacy and
  security concerns and appends new findings to `docs/TODO.md` without
  modifying any source code.
- Imperative #9 in `copilot-instructions.md`: agent must run security-scan
  after any new service, endpoint, or integration is added/modified.
- Initial scan: 13 security/privacy findings logged to `docs/TODO.md`
  (hardcoded Telegram token, unauthenticated API/camera/reboot endpoints,
  ZMQ no-auth, biometric data retention, path traversal in audio API, etc.).


### Fixed
- CI tests now pass on Python 3.11/3.12/3.13 (was failing on every push).
- `ClockAnnouncer._announce` now calls `say_fn` twice at top of hour (time then joke) with an optional `pause_fn` between them, instead of concatenating into one utterance.
- `ClockAnnouncer.__init__` accepts optional `pause_fn` kwarg (used by `clock_service`).
- `test_face_service`: phrase assertions updated to include "VERA" following robot rename.
- `test_skills_service`: `_on_utterance` calls corrected to pass both `_topic` and `payload` args.
- Added `httpx>=0.27.0` to `requirements.txt` (required by FastAPI `TestClient`).
- CI workflow now installs from `requirements.txt` to ensure `psutil` and `httpx` are present.

## [1.23.7] - 2026-05-24
### Fixed
- **Web GUI audio recording — persistent 32 s timeout** — Piper TTS synthesis
  takes ~17–18 s per utterance on Pi 5 (ONNX CPU inference). The audio worker
  was blocking on synthesis every time any speech was requested (startup
  announcement, face greetings, all `av.say` events), starving recording
  requests of their 32 s timeout window. Fixed by separating synthesis from
  playback: synthesis now runs in a single-threaded `ThreadPoolExecutor`
  (`tts-synth`), and only the rendered audio is enqueued to the audio worker
  for playback (~3–5 s). The audio worker stays free for recordings at all
  times. Startup phrase is pre-synthesized during prewarm so playback is
  instant (~4 s total) after the 21 s prewarm+synthesis window.
- **`wait_idle()` correctness** — now waits for both the synth executor and
  the audio worker queue to drain, so test assertions are reliable.

## [1.23.6] - 2026-05-24
### Fixed
- **Web GUI audio recording — 30 s startup delay** — the Piper ONNX TTS model
  was loaded lazily on first speech, blocking the audio worker thread for ~22 s
  during the startup announcement. Any recording request queued in that window
  hit the timeout and returned an error. Fixed by pre-warming the TTS model on
  a background thread (`tts-prewarm`) immediately when `AVService` starts, so
  the model is ready before the audio worker processes the announcement.
- **Thread safety**: `TextToSpeech._load_voice()` now acquires a `threading.Lock`
  to prevent two threads from loading the ONNX model simultaneously.

## [1.23.5] - 2026-05-24
### Fixed
- Recording no longer silently "succeeds" with empty audio: AV recording now
  fails fast when microphone input is unavailable or the captured clip is near
  silence, returning an explicit error instead of writing unusable WAV files.

### Added
- Web GUI recording countdown UX:
  - Progress bar + live seconds-left countdown while recording in the
    Controls -> Audio Clip panel.
- CLI recording countdown UX:
  - `vera record` now shows a live seconds-left countdown in the terminal
    while capture is in progress.

### Changed
- Recording API responses now include input signal metrics (`rms`, `peak`) to
  make low-signal troubleshooting easier.

## [1.23.4] - 2026-05-24
### Fixed
- Recording playback quality: user-recorded WAV clips now bypass TTS loudness
  boost and EQ coloration during playback, preventing heavy distortion and
  over-processed sound on recorded audio.
- AV beep runtime bug: corrected `AudioOutput.beep()` call site to use the
  correct `frequency=` keyword, removing repeated runtime TypeErrors in logs.

### Changed
- `AudioOutput.play()` / `write_chunk()` now accept an `apply_processing`
  flag so callers can choose clean, unprocessed playback when needed.

## [1.23.3] - 2026-05-24
### Added
- Audio clip record/playback support in the core AV pipeline:
  - `AVService` now supports recording microphone input to WAV (`av.record`) and
    playback of latest/specified WAV clips (`av.play_recording`).
  - New bus events: `av.recorded` and `av.recording_played`.
- Web API + GUI controls for audio clip capture/replay:
  - Added `POST /api/audio/record` and `POST /api/audio/playback`.
  - Added dashboard controls for clip duration, recording, and playback with
    status feedback.
- CLI support for the new workflow:
  - Added `vera record [--seconds N] [--output FILE]`.
  - Added `vera playback [--input FILE]`.
  - Updated `vera help` command map to include both commands.
- OpenClaw interface skills for the same flow:
  - Added `.github/skills/record` (`record.py`) and
    `.github/skills/playback` (`playback.py`) with SKILL metadata/docs.

### Changed
- Web service can now locate and call named runtime services through a small
  helper, used by the new audio endpoints to invoke AV operations synchronously.

### Fixed
- Added regression coverage for AV record/playback and web audio endpoints.

## [1.23.3] - 2026-05-24
### Fixed
- **Web GUI audio recording hangs** — `POST /api/audio/record` and
  `POST /api/audio/playback` were calling `av_svc.record_clip()` /
  `av_svc.play_recording()` directly inside `async def` route handlers,
  which blocked the entire uvicorn event loop for the recording duration
  (causing a timeout with no response). Fixed by wrapping both calls with
  `asyncio.to_thread()` so the blocking `queue.Queue.get()` runs off the
  event loop in a thread pool.


### Fixed
- **Fan still not spinning — Pi 5 / RP1 GPIO mux** — `dtoverlay=pwm` is BCM-only
  and cannot mux RP1 GPIO pins on Pi 5. Two-part fix:
  1. Added `ExecStartPre=/usr/bin/pinctrl set 13 a0` to the thermal systemd unit
     so GPIO13 is muxed to `PWM0_CHAN1` (Alt0) before the fan controller starts.
     Without this, lgpio would claim GPIO13 as a software-PWM output and the
     kernel pinctrl would refuse to re-mux it.
  2. Fixed sysfs init race in `fan.py`: writing `period` immediately after
     `export` returns `EBUSY`/`EACCES` on the RP1 PWM driver — added a
     50 ms poll loop (up to 500 ms) after export, and an explicit `enable=0`
     disable step before reconfiguring period.
- Removed ineffective `dtoverlay=pwm,pin=13,func=4` from `/boot/firmware/config.txt`
  (overlay is BCM2711/Pi 4 only; silent no-op on Pi 5 leaves GPIO unmuxed).


### Fixed
- **Fan not spinning after PWM/tach wiring** — root cause was GPIO13 not muxed to
  hardware PWM output. Added `dtoverlay=pwm,pin=13,func=4` to
  `/boot/firmware/config.txt` (requires reboot to take effect). The overlay
  `pwm-2chan` referenced in setup_pi.sh and fan.py was incorrect for this use
  case; corrected to `dtoverlay=pwm,pin=13,func=4` (single-channel, Alt0).
- Corrected overlay reference in `scripts/setup_pi.sh`, `src/thermal/fan.py`
  docstring/warning, and `config/thermal.yaml` comment.
### Added
- `ThermalThresholds.from_yaml()` — thresholds can now be loaded live from
  `config/thermal.yaml` without code changes; `ThermalManager` uses this by
  default.
- Updated `hardware/thermal/TMP117_fan_notes.md` with precise wiring details
  (GPIO pin numbers, pull-up resistor note, power supply guidance).

## [1.23.0] - 2026-05-23
### Changed
- **Project renamed to VERA (Vision-Enabled Reasoning Agent)** across all
  user-visible surfaces:
  - Web GUI: page title and heading now show "VERA"; restart dialog updated.
  - CLI: `prog="vera"`; help banner reads "vera — VERA control CLI";
    `/usr/local/bin/vera` symlink added (existing `da` alias preserved).
  - Face greeting strings: "I'm Desktop Assistant" → "I'm VERA".
  - Boot TTS announcement: "Desktop assistant starting" → "VERA starting".
  - FastAPI title: "Desktop Assistant Dashboard" → "VERA Dashboard".
  - Architecture diagram: title block and `digraph` identifier updated to VERA.
  - All systemd service `Description=` fields, skill files, source docstrings,
    and documentation prose updated.
  - `~/Pictures/vera/` replaces `~/Pictures/desktop-assistant/` as the
    grab-frame save directory.

## [1.22.1] - 2026-05-23
### Fixed
- Mono depth purple screen: scdepthv3 model outputs float32 log-inverse depth
  (all negative values), not uint16 as previously assumed. Normalization now
  uses `(raw - vmin) / (vmax - vmin)` so output is always in [0, 1] regardless
  of the raw value range.
- Web service depth JPEG endpoint: added `np.nan_to_num` + `np.clip(0,1)` guard
  before casting to uint8 to prevent the "invalid value encountered in cast"
  RuntimeWarning and the resulting zeroed-out (purple) image.

## [1.22.0] - 2026-05-23
### Added
- **Face depth labels on cam1 overlay** — VisionService now subscribes to `vision.face_depth` and annotates each face ellipse label with the current range reading (e.g. `Mark  1.43m`). Uses the existing stereo/face-size depth estimate already computed by StereoService; no extra CPU cost.
### Fixed
- **Depth imagery now auto-refreshes in the web GUI** — `refreshDepthMap()` and `refreshMonoMap()` were defined but never called automatically. Added a 3-second `setInterval` that polls while enabled, and `saveDepthSettings()` now fires an immediate refresh when a method is toggled on. `loadDepthSettings()` also refreshes images at page load if already enabled.

## [1.21.8] - 2026-05-23
### Fixed
- **Depth service toggles now work** — `DenseStereoService` and `MonoDepthService` were only instantiated when their respective `dense_enabled`/`mono_enabled` flags were `true` in config, meaning the web GUI toggles (which publish `depth.set_dense_enabled` / `depth.set_mono_enabled` bus events) fired into a void with nobody listening. Both services are now always started at boot; they read their initial enabled state from config and respond to the bus events at runtime to enable/disable processing. When disabled they sleep instead of processing (no CPU waste). `GET /api/settings/depth` now reports the live `_enabled` flag instead of merely checking if the service object exists.

## [1.21.7] - 2026-05-23
### Added
- **Identity stabilisation before greeting** — instead of committing to an identity on the very first ArcFace match (which can be wrong on the first frame), the system now accumulates 8 independent embedding matches before making a final determination. During this window the pos-cache fast path is bypassed so every frame gets a fresh ArcFace lookup. After the window, the majority-vote winner is the committed identity.
- **Contrite correction greeting** — if the majority-vote winner differs from the initial first-frame guess (stabilization_changed=True), FaceService speaks a contrite apology greeting ("Oh wait, I got that wrong — Mark! Sorry about the mix-up…") instead of the normal returning greeting.
- **`face.greeted` bus event** — FaceService now publishes `face.greeted` with `{face_id, name, text, event_type}` on every greeting (new / returning / returning_corrected), enabling TelegramService to relay greetings as designed.
- Perception service now adds `is_stabilizing`, `stabilization_changed`, `initial_name` fields to each `perception.faces` face entry.

## [1.21.6] - 2026-05-23
### Added
- **Cam2 selectable resolution** — cam2 was hard-locked at 640×480. Raised the default capture resolution to 1920×1080, added dedicated `camera2.set_resolution` and `camera2.set_stream_resolution` bus events (previously cam2 accidentally shared cam1's events), added `/api/settings/camera2/resolution` and `/api/settings/camera2/stream_resolution` REST endpoints, and added Capture Resolution / Stream Resolution selectors for cam2 in the web GUI Settings panel. Fixes `/api/snapshot2` returning a downscaled frame by storing the full-res frame before the MJPEG stream downscale.

## [1.21.5] - 2026-05-23
### Fixed
- **Face list Refresh button missing** — when the Re-identify button was moved to the card header in v1.21.2, the bottom action row was left with only the Merge button and no way to manually reload the list. Added a `↺ Refresh` button (calls `loadFaces()`) next to the Merge button so users can reload the face table on demand without waiting for the 30-second auto-poll.

## [1.21.4] - 2026-05-23
### Fixed
- **Watchdog blind to stuck openclaw orphan** — `is_journal_stuck()` queried `journalctl -u openclaw-gateway.service`, but once the systemd service invocation completes (exit 78 "already running"), the orphaned gateway process loses its unit journal tag. The stuck "Bot not initialized" spam was invisible to the unit query (0 hits) while the direct `_PID=` query showed 119 hits in 60s. Refactored `is_journal_stuck()` into a `_journal_pattern_count()` helper; when the unit query returns 0 and `require_systemd_active=False`, it automatically falls back to scanning by the port-holder PID so the stuck loop is detected and triggers a watchdog restart.

## [1.21.3] - 2026-05-23
### Fixed
- **Re-identify button had no effect** — `PerceptionService` never subscribed to `face.refresh`, so clicking Re-identify published the event but the in-memory position cache was never cleared. The position cache kept serving the stale (wrong) identity assignment on every subsequent frame, making Re-identify completely ineffective. Added `_on_face_refresh` handler that clears the pos_cache and calls `registry.reload()`.
- **Position-cache fallback was contaminating galleries** — when `find_match()` returned no match but the position cache had a stale identity at the same position, the code was calling `add_embedding_if_needed()` on that unverified identity. This caused Carson's gallery to re-accumulate Mark's embeddings whenever the re-identification failed. Removed `add_embedding_if_needed()` from the unverified pos-cache fallback path — embeddings are only added after a confirmed `find_match()`.
- **Carson contamination** — cleared Carson's re-accumulated 5 embeddings (added by the above bug after the previous fix cleared the original 24).

## [1.21.2] - 2026-05-23
### Added
- **`FaceRegistry.prune_gallery()`** — retroactively applies the quality gate to all stored embeddings. For each identity, computes the centroid and removes any embedding with cosine similarity below `_QUALITY_GATE_MIN_SIM`. Preserves at least `_QUALITY_GATE_MIN_FRAMES` embeddings per identity. Exposed via `POST /api/faces/refresh` (now also prunes on every Re-identify click) and `da face clean-gallery`.
- **`FaceRegistry.clear_embeddings(face_id)`** — removes all stored embeddings for a single identity without deleting the face entry (name, timestamps, seen_count preserved). Forces a clean re-enrollment under the new quality gate. Exposed via `da face clear-embeddings <name|id>`.
- **`da face clean-gallery`** CLI command — runs `prune_gallery()` on the live database to remove outlier embeddings.
- **`da face clear-embeddings <name|id>`** CLI command — clears all embeddings for one identity by name or ID prefix, forcing re-enrollment from scratch.
### Fixed
- **Web GUI: Re-identify button restored to card header** — moved from the bottom of the face table (scrolled out of view for larger galleries) into the `Face Registry` card `h2` header, where it is always visible alongside Delete All / Remove Guests.
- **Carson mis-identification** — cleared Carson's 24 pre-quality-gate embeddings from the live database. The Carson vs Alaina centroid similarity was 0.734, indicating severe cross-contamination from training sessions where multiple faces were visible simultaneously. Carson's gallery will rebuild cleanly under the new quality gate on next sighting.

## [1.21.1] - 2026-05-23
### Changed
- **Face recognition: per-identity top-K aggregation** — `FaceRegistry.find_match()` now scores a query against every individual stored embedding and takes the mean of the top-K scores (default K=3) per identity, instead of a single mean-prototype vector. This eliminates prototype drift: if a gallery accumulates a few bad embeddings (occluded, blurry, side-profile), the mean prototype drifts away from the person's true appearance and causes missed matches. Top-K aggregation ignores the low-scoring junk and locks onto the best matching gallery frames.
- **Face recognition: embedding quality gate** — `add_embedding_if_needed()` now rejects any new embedding whose cosine similarity to the current gallery centroid is below `_QUALITY_GATE_MIN_SIM` (default 0.20), once at least `_QUALITY_GATE_MIN_FRAMES` (default 5) embeddings are already stored. Side-profile, occluded, and severely blurred faces are silently discarded rather than contaminating the gallery.

## [1.21.0] - 2026-05-23
### Changed
- **Face recognition: per-identity prototype matching** — `FaceRegistry.find_match()` now matches against a per-identity *mean prototype vector* (the L2-normalised mean of all stored embeddings for each person) instead of all individual stored embeddings. Prototype matching is significantly more robust because pose/lighting noise in individual captured frames averages out into a stable representative vector. The prototype matrix is maintained incrementally: updated in O(k) time on every embedding add/replace, so there is no performance regression.
- **Face recognition: raised match threshold 0.40 → 0.50** — the previous threshold was too permissive for ArcFace MobileFaceNet, causing false-positive matches (wrong person recognised). 0.50 is the recommended operating point for this model. Configurable via `face_recognition.match_threshold` in `config/assistant.yaml`.
- **Face recognition: minimum face size filter** — faces narrower or shorter than `min_face_px` (default 80 px) are skipped during embedding and training capture. Tiny/distant faces produce noisy embeddings that pollute the identity gallery and degrade matching. Configurable via `face_recognition.min_face_px` in `config/assistant.yaml`.

## [1.20.6] - 2026-05-22
### Fixed
- **OpenClaw → Telegram "No API key found for provider anthropic"** — the systemd service had no access to `ANTHROPIC_API_KEY`. The key was only set in interactive shell sessions during initial setup and was never persisted to any file the service could read. Added `EnvironmentFile=/home/starter/.openclaw/api-keys.env` to `openclaw-gateway.service`; that file (mode 600, outside the repo) holds the Anthropic API key so it is injected into every systemd-managed gateway process on startup.

## [1.20.5] - 2026-05-22
### Fixed
- **OpenClaw → Telegram feed stalls (the real "finally fix it")** — three coupled bugs that together neutralised the 1.20.1 polling-stall guard:
  1. `desktop-assistant-watchdog.service` was installed but **disabled**, and `config/assistant.yaml` had `watchdog.enabled: false`. The guard logic existed but was never running. The unit is now enabled and the config default flipped to `true`.
  2. `services/systemd/openclaw-gateway.service` had `StartLimitIntervalSec=`/`StartLimitBurst=` under `[Service]`; systemd warns and ignores them there. Moved to `[Unit]`.
  3. When OpenClaw was started manually (e.g. via the `openclaw gateway` CLI), the systemd unit's `ExecStart` would exit 78 ("another instance is healthy") and `systemctl restart` became a no-op for the actual port holder. The watchdog logged "restarted successfully" while the stalled orphan kept the port. `ManagedService.restart()` now detects when the port is held by a PID systemd doesn't own, SIGTERMs it (5 s grace), escalates to SIGKILL if needed, then starts the systemd unit cleanly.

## [1.20.4] - 2026-05-22
### Added
- **Face refresh** across all interaction channels — `POST /api/faces/refresh` web API endpoint; "↺ Re-identify" button in web GUI; `da face refresh` CLI subcommand; OpenClaw `faces` skill (`list`, `status`, `refresh` commands). Reloads the `FaceRegistry` embedding cache and resets `FaceService` tracking state so all faces are re-identified on the next camera frame.

## [1.20.3] - 2026-05-22
### Fixed
- **Web GUI: Pandora `/api/music/status` returning 404** — the route handler function existed but was missing its `@app.get("/api/music/status")` decorator, so FastAPI never registered the endpoint. Music card in the web UI now loads correctly.

## [1.20.2] - 2026-05-22
### Fixed
- **telegram-chime: 409 Conflict / OpenClaw ingress crash** — rewrote `telegram-chime` to watch the OpenClaw update-offset file (`~/.openclaw/telegram/update-offset-default.json`) via `pyinotify` instead of calling `getUpdates` directly. Two simultaneous `getUpdates` pollers on the same bot token caused Telegram 409 Conflict errors every ~5 minutes, which killed OpenClaw's isolated polling ingress (exit code 1) and stopped all Telegram→Claude replies. The new implementation has zero Telegram API calls and plays the chime the moment OpenClaw advances its offset file.

## [1.20.1] - 2026-05-22
### Fixed
- **Watchdog: Telegram polling-stall guard** — added `max_uptime_min` field to `ManagedService`; the watchdog now force-restarts `openclaw-gateway.service` after it has been running for ≥ 90 minutes (configurable via `watchdog.openclaw_max_uptime_min` in `config/assistant.yaml`). This resolves a recurring OpenClaw bug where the Telegram polling ingress silently dies after a long agentic session (multi-minute Claude tool calls), leaving the bot unable to receive new messages until manually restarted.
- Added `openclaw_max_uptime_min: 90` to `config/assistant.yaml` watchdog section.

## [1.20.0] - 2026-05-22
### Added
- **CLI `da depth`** command group: `status`, `dense-enable`, `dense-disable`, `mono-enable`, `mono-disable`, `query`
- **CLI `da snapshot`** command: saves a full-resolution JPEG from camera 1 or 2 to a local file
- **Web GUI Depth card**: toggle dense/mono depth, hardware-ready badges, refreshable depth map images, nearest/farthest/mean stats panel
- **Web API `POST /api/joke`**: triggers TTS random dad joke (was CLI-only)
- **Web API `POST /api/time`**: triggers TTS time announcement (was CLI-only)
- **OpenClaw skill `random-motion`**: enable/disable/status idle random head movement
- **OpenClaw skill `version`**: ask assistant to speak its version number
- **OpenClaw skill `power`**: reboot or shutdown with mandatory `--confirm` flag
- **OpenClaw skill `joke`**: tell a random dad joke via TTS
- **OpenClaw skill `time`**: announce the current time via TTS
- All 5 new skills deployed to `~/.openclaw/workspace/skills/`

## [1.19.0] - 2026-05-21
### Added
- **Hailo-8 monocular depth (Phase 2)**: `MonoDepthService` runs `scdepthv3.hef` on the Hailo-8 at up to 3 Hz on a single camera frame. Outputs a normalised relative depth map `[0,1]` published on `vision.mono_depth_map`. Optional `mono_scale_factor` config converts to approximate metres.
- **Web API `GET /api/depth/mono`**: Colorized TURBO JPEG of the monocular depth map.
- **`GET /api/settings/depth`**: Now reports `mono_enabled` and `mono_hardware_ready` accurately (was placeholder `false`).
- **Config**: `mono_hef_path` and `mono_scale_factor` fields added to `config/assistant.yaml`.
- **12 new tests** in `tests/test_mono_depth_service.py` (all passing).

## [1.18.1] - 2026-05-21
### Fixed
- **Watchdog OpenClaw restart loop (again)**: `SuccessExitStatus=78` makes systemd treat the "already running" exit as clean, but the unit still shows `inactive (dead)` — causing `systemctl is-active` to return non-zero. The watchdog's systemd-active check short-circuited before the HTTP check, so it always saw OpenClaw as unhealthy and restarted every 5 minutes. Fixed by adding `require_systemd_active: bool = True` to `ManagedService` and setting it `False` for `openclaw-gateway.service`, so OpenClaw health is determined solely by its HTTP `/health` endpoint.

## [1.18.0] - 2026-05-21
### Added
- **Dense stereo depth estimation (Phase 1)**: Per-pixel depth map for everything in view using OpenCV `StereoSGBM`. New `DenseStereoService` runs in background at up to 3 Hz, publishes `vision.depth_map` on the bus. Includes `StereoRectifier` (loads `config/stereo_cal.npz`) and `DenseStereoMatcher` (SGBM, converts disparity to metric depth). Degrades gracefully without calibration file.
- **Stereo calibration script**: `scripts/calibrate_stereo.py` — interactive tool to capture checkerboard pairs and compute stereo camera calibration.
- **Web API depth endpoints**: `GET /api/depth/map` (colorized TURBO JPEG), `GET /api/depth/query` (stats + per-face depths JSON), `GET/PUT /api/settings/depth` (enable/disable at runtime).
- **OpenClaw skill: `depth-query`**: Query nearest/farthest/mean scene depth and per-face distances. Installed to `~/.openclaw/workspace/skills/depth-query/`.
- **OpenClaw skill: `depth-toggle`**: Enable or disable dense/mono depth estimation at runtime. Installed to `~/.openclaw/workspace/skills/depth-toggle/`.
- **Internal skill: `DepthQuerySkill`** (`src/skills/depth_query_skill.py`): Matches spoken depth queries and dispatches to the depth service.
- **Tests**: `tests/test_dense_stereo.py` — 14 tests covering `DenseStereoMatcher`, `StereoRectifier`, and `DenseStereoService`.
- **Config**: `depth.dense_enabled`, `dense_rate_hz`, `dense_width`, `dense_height`, `num_disparities`, `block_size`, `mono_enabled`, `mono_rate_hz` added to `config/assistant.yaml`.

## [1.17.2] - 2026-05-21
### Fixed
- **OpenClaw watchdog restart loop**: A stale `nohup`-launched OpenClaw process (PID from prior session) was holding port 18789, causing every systemd-managed instance to exit with code 78 (`EADDRINUSE`). Systemd treated exit 78 as a failure and restarted, which the watchdog then also saw as "failed" — creating a perpetual restart storm. Added `SuccessExitStatus=78` to `openclaw-gateway.service` so systemd treats exit 78 as a clean "already running" signal (as OpenClaw intends). Killed the stale rogue process; OpenClaw now runs as a single systemd-owned instance.

---


### Fixed
- **Watchdog HTTP health check false failures**: The watchdog was checking `http://localhost:8080/api/status` for `{"ok": true}` but the status endpoint returns a full telemetry payload with no `ok` field — causing every check to fail and the core service to be restarted every 5 minutes. Added a dedicated `GET /health` endpoint to the web service that returns `{"ok": true, "status": "live"}` and updated the watchdog to check that URL instead.

---

## [1.17.0] - 2026-05-21
### Added
- **System health watchdog** (`src/watchdog/watchdog.py`): A new Python daemon that monitors all three critical services every 30 seconds and auto-restarts anything that fails or gets stuck. Checks include: systemctl unit state, HTTP health endpoint, and a journal scan for OpenClaw's "Bot not initialized" stuck-loop pattern. Each service has an independent 5-minute restart cooldown to prevent restart storms. Sends a Telegram notification (🩺) on every auto-fix.
- **`openclaw-gateway.service`** systemd unit: OpenClaw is now a proper supervised systemd service (`Restart=on-failure`) instead of a bare `nohup` background process. Auto-starts on boot, auto-restarts on crash.
- **`desktop-assistant-watchdog.service`** systemd unit: Watchdog runs as a supervised service (`Restart=always`) so the watchdog itself is also self-healing.
- Both new units are enabled and running. Config under `watchdog:` in `config/assistant.yaml`.


### Added
- **Telegram face notifications**: Every face-activity audio output (new-face greeting, returning-face greeting, name assignment) is now also sent as a Telegram message to the configured chat. `FaceService` publishes a new `face.greeted` bus event whenever it speaks a greeting (new_face / returning / named). A new `TelegramService` subscribes to `face.greeted` (and a generic `telegram.send` topic) and forwards the text via the Telegram Bot API on a background thread. Config lives under `telegram:` in `config/assistant.yaml`. Emojis: 👋 new face, 👤 returning, 🏷️ named.


### Fixed
- **Top-of-hour pause**: The time announcement and the dad joke no longer run together without a break. Added `av.silence` topic to `av_service.py` that enqueues a timed `time.sleep()` in the audio worker queue. `ClockAnnouncer` now accepts an optional `pause_fn` parameter; at the top of the hour it calls `say_fn(time_str)` → `pause_fn()` → `say_fn(joke)` instead of concatenating both into one utterance. `ClockService` wires a 1.5-second pause via `bus.publish("av.silence", {"duration": 1.5})`.

## [1.16.0] - 2026-05-18
### Added
- **Face merge parent/child modal**: Replaced the plain `confirm()` dialog with a full modal that shows both faces side-by-side with thumbnails, names, and radio buttons to choose which is the parent (kept) and which is the child (absorbed/deleted). Includes a ⇄ swap button and a live summary line before confirming. Green border on the kept face, red/dimmed on the absorbed face.

## [1.15.9] - 2026-05-17
### Added
- **7 new OpenClaw skills** for natural-language Telegram/Claude control:
  - `say` — speak any text via TTS
  - `describe-scene` — trigger spoken scene description from camera
  - `music` — full Pandora playback control (play/stop/next/pause/thumbs-up/thumbs-down/volume/stations/station)
  - `face-tracking` — enable/disable/status face-following servo behavior
  - `system-status` — health telemetry: CPU, memory, temp, FPS, services, faces
  - `quiet-hours` — enable/disable/configure TTS silence window
  - `object-detection` — enable/disable Hailo-8 COCO object classifier
- Updated `.github/skills/README.md` with full skill reference table and bulk install command.

## [1.15.8] - 2026-05-17
### Changed
- Removed duplicate `openclaw/skills/` top-level directory; canonical location for OpenClaw skills is `.github/skills/` (shared with Copilot CLI skills).
- Updated `README.md` repo layout to include `.github/skills/` and link to its README for OpenClaw setup.

## [1.15.7] - 2026-05-17
### Added
- **OpenClaw skills added to repo** (`openclaw/skills/`): `pan-camera` and `grab-frame` skills are now version-controlled alongside the rest of the project. Includes `openclaw/README.md` with installation instructions.
- Updated `README.md` repo layout and added OpenClaw integration section.

## [1.15.7] - 2026-05-17
### Added
- **OpenClaw skills checked into repo** under `.github/skills/`: `pan-camera` (servo pan via Telegram) and `grab-frame` (full-res camera still). Includes a `README.md` with deployment and cache-clearing instructions.

## [1.15.6] - 2026-05-17
### Fixed
- **Servo pan silently blocked during quiet hours**: All `motion.pan_to` commands (CLI, web GUI, OpenClaw/Telegram) were suppressed when quiet hours were active (21:00–05:00). Added `"override_quiet": true` payload flag so explicit user commands bypass quiet hours while autonomous tracking/random-motion stays silent. Updated `scripts/desktop-assistant`, `web_service.py`, and `pan-camera` OpenClaw skill.
### Added
- **`/api/snapshot` and `/api/snapshot2` endpoints**: Return the current full-resolution JPEG from camera 1 or camera 2 respectively (quality 95, encoded from raw frame without stream downscaling).
- **`grab-frame` OpenClaw skill**: Telegram/Claude can now take a full-res still photo from either camera via `grab_frame.py [1|2]`. Files saved to `~/Pictures/desktop-assistant/cam<N>_<timestamp>.jpg`.

## [1.15.5] - 2026-05-16
### Fixed
- **Stream resolution only applied to cam1**: `RawCameraService` (cam2) had no stream downscale logic. Added `stream_width`/`stream_height` fields to `RawCameraConfig`, AR-preserving resize in `run_tick`, and `_on_set_stream_resolution` handler. Both cameras now subscribe to the same `camera.set_stream_resolution` bus message, so the single "Stream Resolution" GUI selector controls both feeds simultaneously.
### Added
- `stream_width`/`stream_height` fields in `RawCameraConfig` (default 640×360).
- `stream_resolution` property on `RawCameraService`.
- Cam2 stream dims wired through `core_main.py`.

## [1.15.4] - 2026-05-16
### Fixed
- **Resolution selector zooms/crops image**: Changing camera resolution via the GUI restarted Picamera2 at a lower resolution, causing the Pi Camera ISP to use a center-cropped sensor mode instead of a full-FOV downscale. Fixed by separating capture resolution from stream/display resolution: the GUI now controls the MJPEG stream output size (`camera.set_stream_resolution` bus message) which only changes the encoder's downscale target. The camera always captures at its configured full-FOV resolution; no restart occurs when the user changes "Stream Resolution".
### Added
- `GET/PUT /api/settings/camera/stream_resolution` endpoints to control MJPEG stream output resolution independently of capture resolution.
- `_on_set_stream_resolution()` handler in `VisionService` — updates `_stream_width`/`_stream_height` live; encoder picks up the change on the next frame.
- `stream_width: 640` / `stream_height: 360` defaults in `config/assistant.yaml` and wired through `core_main.py → CameraConfig`.
- GUI selector renamed "Stream Resolution" with 16:9-native presets (640×360 default).
### Changed
- `_encoder_loop` reads `self._stream_width/height` each frame (was read once at thread start) so live stream-resolution changes take effect immediately without a daemon restart.

## [1.15.3] - 2026-05-16
### Fixed
- **Detection cam stream stretched**: `_encoder_loop` was resizing to exactly `(stream_width, stream_height)` = 640×480 regardless of capture aspect ratio, squishing 1920×1080 (16:9) into 4:3. Replaced with aspect-ratio-preserving resize: the display frame is fitted to the stream bounds while preserving the source ratio (1920×1080 → 640×360, not 640×480).
- **Servo direction overlay double height**: Arc in cv2 angle convention spans 210°→330° which renders *above* the center point (cy). The heading label was placed at `cy + radius` — far below the center dot — creating a large empty gap and inflating the block height. Moved heading label to `cy + dot_r + 4` (just below the center dot) and tightened `by2` accordingly. Block height roughly halved at all scale factors.

## [1.15.2] - 2026-05-16
### Fixed
- **cam1 FPS (GIL contention)**: Hailo SCRFD face-detection was running at 10 fps (default), each inference holding the Python GIL for ~50ms → 500ms of GIL-blocked time per second, starving the encoder thread. Added `face_detection.max_fps` config (default `5.0`) to `config/assistant.yaml` and wired it through `core_main.py` → `PerceptionConfig`. Halving detection rate frees ~250ms/s of GIL for the encoder, expected cam1 FPS improvement from ~6fps to ~12–15fps.
- **Stream encoder efficiency**: moved `cv2.resize` to before overlay drawing — Hailo detection still receives full capture-res frames; all overlay drawing now happens on the smaller stream frame. Eliminates the large `frame.copy()` call on the 1920×1080 frame.
### Added
- `_scale_bboxes()` helper: scales face/object detection bbox coordinates when stream resolution differs from capture resolution.
- `face_detection.max_fps` config in `config/assistant.yaml`.

## [1.15.1] - 2026-05-16
### Fixed
- **cam1 FPS regression (JPEG encode cost)**: JPEG encoding a 1920×1080 frame took 33–115ms, capping cam1 at ~8fps even after the GIL and bus-lock fixes. Added `stream_width`/`stream_height` to `CameraConfig` (default 640×360). The encoder resizes the display frame to the stream resolution before `cv2.imencode` — Hailo face detection continues on full capture resolution.
### Added
- `stream_width: int` / `stream_height: int` fields to `CameraConfig` dataclass.

## [1.15.0] - 2026-05-16
### Fixed
- **cam1 FPS regression (PIL GIL starvation)**: `_draw_servo_overlay` called PIL 3× per frame while Hailo inference held the GIL, causing 94–417ms stalls. Pre-render static overlay elements once into a cached BGRA patch; composite via `cv2.copyTo` with uint8 mask (~1ms vs ~15ms with float32 blend). Heading label cached per integer degree. Hot-path overlay time: ~4ms/frame (was ~417ms under load).
### Added
- `_build_servo_bg_patch()`: builds static arc + limit-label overlay patch once per unique (w, h, servo_min, servo_max) key.
- `_render_hdg_patch()`: pre-renders heading angle label once per integer degree using PIL (for degree symbol support).
- `_servo_bg_cache`, `_servo_hdg_cache`: module-level caches cleared on servo limit changes or frame resolution change.

## [1.14.7] - 2026-05-16
### Added
- **Drag-and-drop card reordering** in the web dashboard. Grab the `⠿` handle in any card's header to drag it to a new position. Layout is persisted in `localStorage` so the arrangement survives page refresh. Dragging only activates from the handle, leaving all inputs, sliders, and buttons inside cards fully interactive.

## [1.14.6] - 2026-05-15
### Changed
- Direction overlay labels now use **PIL TrueType** (DejaVu Sans) instead of OpenCV Hershey font, enabling proper rendering of the Unicode degree symbol (`°`). Labels show e.g. `135°`, `215°`, `175°`.
- Added `_put_text_pil()` helper and `_pil_font()` LRU cache for reuse across frames.

## [1.14.5] - 2026-05-15
### Fixed
- Direction overlay labels no longer show `??` — replaced Unicode degree symbol (`\u00b0`) with ASCII `d` suffix since OpenCV Hershey fonts are ASCII-only.
### Changed
- Added semi-transparent dark background panel (`addWeighted`, 55% opacity) behind the arc compass widget for high contrast against any background.

## [1.14.4] - 2026-05-15
### Changed
- **Head position overlay** (arc compass, bottom-right of video feed):
  - Arc and pointer are now 50% larger (radius `40→60` scaled units).
  - Limit angle labels (`servo_min°` / `servo_max°`) shown at the arc endpoints in gray.
  - Current heading angle label shown in cyan below the arc centre.
  - Removed the redundant `"Pan: X°"` text label that appeared bottom-left.

## [1.14.3] - 2026-05-14
### Added
- **Stereo depth localization**: dual-method depth estimation now wired into `perception.faces` payload (`depth_m`, `pos_3d` per face).
  - *Face-size method* (always-on): `Z = focal_px × face_width_m / bbox_width_px` using configured FOV + 0.145 m average face width; accurate ±15% for 0.3–4 m frontal faces.
  - *Stereo template method*: `StereoService` subscribes to `perception.faces`, grabs both camera frames, runs `cv2.TM_CCOEFF_NORMED` template matching, computes disparity from cam1→cam2 horizontal offset; `Z = focal_px × baseline_m / disparity_px`. 56 mm baseline → ~15 px disparity at 1 m.
- **`src/perception/depth_estimator.py`** — utility library: `focal_px_from_fov`, `face_size_depth`, `stereo_depth_from_disparity`, `to_3d`, `StereoFaceMatcher`.
- **`src/services/stereo_service.py`** — event-driven `StereoService` with `StereoConfig`; publishes `vision.face_depth`.
- Depth display in face-tracking video overlay (e.g., `"Guest 1  1.23m"`).
- `depth:` config section in `config/assistant.yaml` (baseline_mm, known_face_width_m, min/max depth).
- `RawCameraService.latest_frame()` — thread-safe access to latest raw numpy frame for stereo matching.
- `tests/test_depth_estimator.py` — 13 unit tests for all depth utilities.

## [1.14.2] - 2026-05-13
### Added
- **Live head-tracking tuning UI** (Web Dashboard → "Head Tracking Tuning" card): 11 sliders for `tracking_gain`, `dead_zone_frac`, `max_speed_deg_s`, Kalman `r`/`q_pos`/`q_vel`, `lookahead_s`, `replan_threshold_deg`, and min-jerk `move_base_s`/`move_scale_s_per_deg`/`move_max_s`. Changes apply live without a restart.
- **Presets**: Default / Snappy / Smooth dropdown; one-click apply.
- **Save to config / Reset to defaults** buttons; saves persist via `ruamel.yaml` (comments preserved).
- **Guided auto-tune** (~20 s, 2-stage):
  - Stage 1 (5 s, "hold still"): measures face-detection noise σ → sets `kalman_r ≈ (3σ)²`.
  - Stage 2 (~16 s, "wave head"): probes 4 candidate gains, computes lag via cross-correlation and overshoot → picks min-scoring `tracking_gain`.
- **Live telemetry chart** (last 5 s, ~10 Hz): face_raw, face_smoothed, target_angle, servo_angle on a Canvas 2D strip via `/ws/tracking-debug` WebSocket.
- New REST endpoints under `/api/tracking/*` (`params` GET/POST, `save`, `reset`, `preset`, `autotune/start`, `autotune/cancel`) and a `/ws/tracking-debug` WebSocket.
- New bus topics: `tracking.set_param`, `tracking.get_params`, `tracking.save_params(_done)`, `tracking.reset_params`, `tracking.apply_preset`, `tracking.preset_applied`, `tracking.start_autotune`, `tracking.cancel_autotune`, `tracking.autotune_progress`, `tracking.autotune_done`, `tracking.param_changed`, `tracking.debug`.

### Changed
- `HeadTracker` now exposes `update_config(name, value)`, `get_config()`, and `get_debug_state()` for live tuning + telemetry; whitelist-and-range validated.
- `FaceKalman` exposes `r`, `q_pos`, `q_vel` as live-settable properties so Kalman noise can be mutated without re-instantiating.
- `TrackingService` publishes `tracking.debug` at 10 Hz and runs the auto-tune state machine inside the 20 Hz control loop.
- Added `ruamel.yaml>=0.17` to `requirements.txt` (preserves comments when persisting tuned values to `config/assistant.yaml`; falls back to PyYAML if missing).

### Tests
- `tests/test_head_tracker.py`: added coverage for `update_config` whitelist/bounds, Kalman propagation, `get_config`/`get_debug_state`.
- `tests/test_tracking_autotune.py` (new): cross-correlation lag detection on synthetic sinusoidal data; YAML round-trip with `_persist_head_tracking_params`.

---

## [1.14.1] - 2026-05-13
### Changed
- Replaced spring-damper head-tracking controller with **Kalman filter + minimum-jerk trajectory planner** for smoother, human-like motion profiles.
  - `FaceKalman` (new `src/motion/face_kalman.py`): 1-D Kalman filter [position, velocity] estimates face centroid and velocity, enables lookahead prediction.
  - `_MinJerkPlanner` inside `head_tracker.py`: 5th-order polynomial (Flash & Hogan 1985) for smooth saccade and idle gaze movements; replans from current position+velocity to preserve momentum.
  - Removed `spring_k`, `damping`, `face_ema_alpha` from `HeadTrackerConfig`; added `kalman_r`, `kalman_q_pos`, `kalman_q_vel`, `lookahead_s`, `replan_threshold_deg`, `move_base_s`, `move_scale_s_per_deg`, `move_max_s`.
  - `max_speed_deg_s` raised from 60→250 to allow natural saccade speeds; replan threshold prevents constant replanning on noisy face detections.
- Updated `config/assistant.yaml` `head_tracking:` section with new Kalman and min-jerk parameters.
- Updated `src/assistant/core_main.py` to construct `HeadTrackerConfig` with new parameters.
- Updated `tests/test_head_tracker.py`: removed obsolete spring-damper fixture params; added min-jerk smoothness and face-lost deceleration tests.
- Added `tests/test_face_kalman.py`: coverage for Kalman init, smoothing, velocity tracking, reset, predict, and variable dt.

## [1.14.0] - 2026-05-13
### Added
- Face registry **📷 Train** button: captures the current camera frame, runs face
  detection, generates an ArcFace embedding, and adds it to the selected identity's
  training data in FaceRegistry. Updates the face thumbnail with the new crop.
- `PerceptionService.capture_training_image(face_id)` method handles the
  frame grab → detect → embed → registry write pipeline.
- `POST /api/faces/{id}/train` REST endpoint in WebService; returns embedding/
  thumbnail update status. Publishes `face.training_capture` bus event on success.
- `btn-train` CSS class added to dashboard stylesheet.

---

## [1.13.3] - 2026-05-13
### Fixed
- `AudioInputConfig.sample_rate` default changed from 16000 → 44100 Hz to match
  CM108 USB audio adapter's native rate; prevents PortAudio probe failure and
  sim-mode fallback when using Sabrent AU-MMSA.
- `scripts/test_microphone.py` WAV writer now uses 44100 Hz sample rate to
  match the actual capture rate (WAV was previously written with wrong header).

---

## [1.13.2] - 2026-05-13
### Fixed
- CI workflow: added `libportaudio2` system dep and `opencv-python-headless`,
  `pyyaml`, `fastapi` Python packages so all test modules can be collected on
  the Ubuntu GitHub Actions runner (previously all 3 Python matrix jobs failed
  at collection with `ModuleNotFoundError: cv2 / yaml / fastapi` and an
  `OSError: PortAudio library not found`).

## [1.13.1] - 2026-05-13
### Fixed
- `SkillsService._on_utterance` was missing the `_topic` parameter, causing a
  `TypeError` on every utterance dispatch — skills never fired from web GUI or
  voice input.

## [1.13.0] - 2026-05-13
### Added
- **Skill config framework** (`src/skills/base.py`) — `ConfigField` dataclass
  (`name, label, type, default, description, options, min, max, secret`) lets skills
  declare typed configuration fields.  `Skill.enabled` flag; disabled skills are
  skipped by `SkillRegistry.dispatch()`.  Optional `start(bus)` / `stop()` lifecycle
  hooks for background-threaded skills.  `SkillRegistry.find(name)` by-name lookup.
- **WeatherSkill** (`src/skills/weather_skill.py`) — "what's the weather", "will it
  rain", "temperature outside" → fetches current conditions from `wttr.in` (no API
  key).  Config: `location` (default: auto-detect), `units` (imperial/metric).
- **ReminderSkill** (`src/skills/reminder_skill.py`) — "remind me to X in N
  minutes/hours" / "at HH:MM" → background thread fires `av.say` when due.  "list
  reminders", "clear all reminders" commands.  Config: `snooze_min`, display-only
  `pending` field.
- **SmartHomeSkill** (`src/skills/smart_home_skill.py`) — Home Assistant REST API
  stub for "turn on/off <device>", "set thermostat to N", "lock the front door".
  Disabled by default (requires HA config).  Config: `base_url`, `token` (secret),
  `default_room`.
- **NewsSkill** (`src/skills/news_skill.py`) — "what's in the news", "top headline",
  "any news today" → fetches headlines from configurable RSS feed via stdlib only.
  Config: `feed_url` (default: BBC News), `max_headlines`.
- **Web GUI skill config panel** — skills table now has Enabled toggle switch and ⚙
  button per skill.  Clicking ⚙ expands an inline form auto-generated from
  `config_schema` (bool/int/float/str/select/display field types).  Save POSTs to new
  REST endpoints.
- **REST endpoints** — `POST /api/skills/{name}/enabled` (toggle), `GET /api/skills/
  {name}/config`, `POST /api/skills/{name}/config` (per-field update).  `GET
  /api/skills` now returns `enabled`, `has_config`, `config_schema`, `config_values`.
- **CLI** — `da skills enable <name>`, `da skills disable <name>`, `da skills config
  <name>` (show), `da skills config <name> key=value …` (set).  `da help` updated.
- **Tests** — `test_skill_config_framework.py`, `test_weather_skill.py`,
  `test_reminder_skill.py`, `test_smart_home_skill.py`, `test_news_skill.py` (69 new
  tests; total 476 tests, all passing).

## [1.12.0] - 2026-05-13
### Added
- **HelpSkill** (`src/skills/help_skill.py`) — "what can you do", "list skills",
  "help" → spoken capability summary.
- **SystemStatusSkill** (`src/skills/system_status_skill.py`) — "how hot are you",
  "what's the temperature", "system status" → reports live CPU temp, fan duty, and
  CPU usage.  Live data is injected via a shared dict updated by SkillsService when
  `thermal.temp` arrives.
- **QuietHoursSkill** (`src/skills/quiet_hours_skill.py`) — "enable/disable quiet
  hours", "are you in quiet mode" → toggles quiet hours via the QuietHours object and
  publishes `settings.quiet_hours_updated`.
- **VolumeSkill** (`src/skills/volume_skill.py`) — "set volume to 60", "louder",
  "mute" → publishes `music.set_volume {"level": int}` or `{"delta": int}`.
- **MusicService**: added `music.set_volume` bus subscription; supports both absolute
  `{"level": int}` and relative `{"delta": int}` payloads.
- **AVService**: publishes `av.speaking_started {"text": str, "ts": float}` immediately
  before each TTS utterance (was previously only publishing `av.spoke` after completion).
- **TrackingService**: subscribes to `av.speaking_started` / `av.spoke`; adds a
  sinusoidal nod offset (configurable amplitude + frequency) to the servo target while
  DA is speaking, making it look more alive.  Config under `head_tracking.speaking_motion`.
- **NotificationService** (`src/services/notification_service.py`) — proactive speech
  service: speaks thermal warnings at configurable thresholds, and a check-in message
  if no face has been seen for `absence_min` minutes.  Rate-limited per notification
  type; respects quiet hours.
- **`GET /api/skills`** endpoint in WebService — returns name + example phrase for all
  registered skills.
- **`POST /api/utterance`** endpoint in WebService — dispatches text directly to the
  skills engine (same as speaking it).
- **Skills panel** in web dashboard — collapsible panel lists all registered voice
  skills with example phrases and a text box to dispatch utterances for testing.
- **`da skills list`** CLI command — lists all registered voice skills with example
  phrases from `GET /api/skills`.
- `config/assistant.yaml`: new `notifications:` section (thermal_alerts, absence_alerts)
  and `head_tracking.speaking_motion` sub-section.
- Architecture diagram updated for NotificationService, speaking motion, new API
  endpoints, and 13-skill SkillsService.
- **`SkillRegistry.skills`** property (read-only list view of registered skills).

### Changed
- `SkillsService`: accepts `quiet_hours=` parameter (passed to QuietHoursSkill);
  subscribes to `thermal.temp` to keep `_live_data` dict fresh for SystemStatusSkill;
  exposes `registry` property so WebService can enumerate skills.
- `WebService`: accepts `skills_service=` constructor parameter.
- `core_main.py`: passes `skills_service=skills_svc` to WebService; wires
  NotificationService with config from `notifications:` YAML section; passes
  speaking-motion config to TrackingService.

## [1.11.3] - 2026-05-12
### Fixed
- **Face deletion in-memory purge**: deleting a face (single, guest bulk, or
  registry clear) now immediately evicts that face from `PerceptionService._pos_cache`
  and from `FaceService` in-memory sets (`_greeted_new_ids`, `_prev_face_ids`,
  `_absent_counter`).  Previously the deleted face could be re-matched for up to
  10 s from the position cache and would never receive a fresh greeting.
- `WebService` now publishes `face.deleted {"face_id": …}` on `DEL /api/faces/{id}`.
  Previously only bulk-delete endpoints published bus events.
- `WebService` now includes `face_ids` list in the `face.guests_cleared` payload so
  subscribers can surgically remove only the deleted IDs.
- `PerceptionService._pos_cache` access is now protected by `_pos_cache_lock`
  (threading.Lock) to prevent races between the detection worker thread and bus
  event handlers calling `_find_cached_face` / `_update_pos_cache`.
- `FaceRegistry.delete_guest_faces()` now returns `(count, [ids])` instead of just
  `count` so the WebService can publish the deleted face IDs.

## [1.11.2] - 2026-05-12
### Fixed
- `SkillsService` was overriding `start()` / `stop()` directly, bypassing the
  `Service` base-class thread machinery.  Renamed to `on_start()` / `on_stop()`
  so `_running` is set correctly and the boot self-test no longer reports
  "service did not start".


### Added
- **Person-seek tracking**: when `perception.objects` contains a `person` detection
  and no face is currently locked, `TrackingService` pans toward the person's
  horizontal centre using the same spring-damper path as face tracking.  Once
  SCRFD picks up a face the face lock takes over immediately.  Person hints
  expire after 2 s (≈ 4 missed detection frames).
- `person_seek_enabled: true` config flag in `config/assistant.yaml` under
  `head_tracking`.  Toggleable at runtime via `tracking.set_person_seek` bus
  topic; changes are broadcast on `tracking.person_seek_changed`.
- 13 new tests in `tests/test_tracking_service.py` covering hint population,
  staleness, face-takes-priority, and toggle semantics.


### Added
- **Skills framework** (`src/skills/`): `Skill` ABC and `SkillRegistry` — voice-intent
  dispatch system that maps natural-language utterances to assistant actions.
  First-match-wins ordering; skills return a spoken string or `None` (silent).
- **SkillsService** (`src/services/skills_service.py`): subscribes to `av.utterance`,
  builds the skill registry at startup, and dispatches incoming utterances.
  Wired into `core_main.py`.
- **9 built-in skills**:
  - `GreetingSkill` — time-aware greetings (morning/afternoon/evening/generic buckets)
  - `TellTimeSkill` — speaks current local time in 12-hour AM/PM format
  - `TellJokeSkill` — publishes `av.tell_joke` → ClockService fetches and speaks a joke
  - `MeetFaceSkill` — publishes `face.meet` → FaceService registers a name for the visible face
  - `DescribeSceneSkill` — publishes `vision.describe` → ObjectService speaks scene description
  - `MotionControlSkill` — "look left/right/center" → `motion.pan_to` at 145°/215°/180°
  - `MusicControlSkill` — play/stop/pause/skip/thumbs-up/thumbs-down → `music.*` topics
  - `ObjectDetectToggleSkill` — enable/disable YOLOv8s object detection via `object.set_enabled`
  - `FaceTrackingToggleSkill` — "follow me" / "stop following me" → `tracking.set_face_tracking`
- **70 new tests** in `tests/test_skills.py` and `tests/test_skills_service.py`.
- **Architecture diagram updated** to include `SkillsService`, `ObjectService`, `MusicService`,
  and `perception/object_detector.py` driver node (all previously missing).  Rebuilt to
  `.pdf`, `.svg`, `.png`.

## [1.10.0] - 2026-05-12
### Added
- **`max_fps`, `conf_threshold`, `max_objects` now configurable** via `config/assistant.yaml`
  under the `object_detection` key.  All three parameters were previously hardcoded.
- **`max_objects` cap** (default 8): detections are sorted by confidence and only the top
  `max_objects` are sent to the overlay, limiting encoder overlay work when many objects
  are visible.

### Changed
- **Object detection default FPS reduced from 3.0 → 2.0** to halve Hailo-8 scheduling
  contention between YOLOv8s (object) and SCRFD (face) inference slots.
- **Letterbox geometry is now cached** in `ObjectDetector`.  Scale, padding, and new
  dimensions are recomputed only when the source frame dimensions change (e.g. resolution
  switch); `buf.fill(0)` is likewise deferred to geometry changes only.
- **`_decode()` reuses cached letterbox params** instead of independently recomputing
  `scale / pad_top / pad_left` on every call.
- **BGR → RGB correction in `_letterbox()`**.  YOLOv8s expects RGB input; the camera
  delivers BGR.  The channel swap is now applied during letterbox copy via a NumPy view
  (`resized[:, :, ::-1]`) — no extra allocation.
- **Rate-limit pre-check in `ObjectService._on_frame_ready()`**: incoming `vision.frame_ready`
  signals are discarded when the last detection was too recent (< 90% of `min_interval`).
  This drops worker thread wakeups from 30/sec to ≈2/sec, reducing thread-scheduling
  overhead and lock contention on the frame queue.

## [1.9.9] - 2026-05-12
### Fixed
- **Encoder thread framerate regression (30fps → 15fps)** caused by thick-stroke
  `cv2.putText` outlines introduced in v1.9.5–v1.9.6.  A `putText` with
  `thickness=5` at font_scale 1.1 is ~25× more expensive than `thickness=1` due to
  the O(thickness²) per-glyph rasterization cost.
- Replaced stroke outline with a fast **bit-shift darken rect** (`roi >> 1`) behind
  the text — same visual contrast, no per-pixel font rasterization overhead.
- Reduced `font_thick` from `max(1, round(2*scale))` to `max(1, round(scale))` at
  all three draw sites (face labels, object labels, pan overlay).
- Removed the redundant manual darken-ROI from the pan overlay (now handled
  internally by `_put_text_outlined`).

## [1.9.8] - 2026-05-12
### Fixed
- **CPU spike caused by embedding cache full-rebuild on every face detection frame.**
  `add_embedding_if_needed()` previously called `_invalidate_emb_cache()` on every
  successful match, forcing a full SQLite read + `np.stack()` on the next `find_match()`
  call (~50–100 ms at ~10 fps = near-continuous load).
  - Added `_emb_row_ids` list (parallel to `_emb_matrix`) to enable targeted row lookup.
  - Added `_append_to_cache()`: appends one row with `np.vstack` — no SQLite touch.
  - Added `_replace_in_cache()`: replaces one row in-place (the prune-and-replace path);
    matrix shape unchanged, zero allocation.
  - `add_embedding_if_needed()` / `add_embedding()` now update the cache incrementally.
  - `register()` appends to cache instead of invalidating.
  - `set_name()` patches `_emb_names` in-place instead of invalidating.
  - Full cache rebuild (`_invalidate_emb_cache`) is now reserved for structural changes
    only: `delete_face()` and `clear()`.

## [1.9.7] - 2026-05-12
### Changed
- `_put_text_outlined()`: black stroke outline is only drawn when the text color is
  light (dark background). On bright backgrounds the dark text is rendered without
  an outline, which was previously muddying legibility.

## [1.9.6] - 2026-05-12
### Changed
- Overlay text color is now adaptive: `_bg_luminance()` samples the BT.601 perceived
  luminance of the text bounding-box ROI; `_contrast_color()` returns near-black on
  bright backgrounds or a brightened accent on dark backgrounds.
- `_put_text_outlined()` now performs the luminance sample internally — callers pass
  an accent hint instead of a fixed color.
- Pan-angle label migrated to `_put_text_outlined()` (was a bare `cv2.putText`).

## [1.9.5] - 2026-05-12
### Changed
- Overlay text size increased: face labels `max(0.8, 1.1×scale)`, object labels
  `max(0.8, 1.1×scale)`, pan overlay `max(0.7, 1.1×scale)` — up from 0.55/0.75×.
- Added `_put_text_outlined()` helper — draws a thick black stroke behind all face
  and object labels so text is readable on any background colour.

## [1.9.4] - 2026-05-12
### Added
- `src/perception/scrfd_decode/scrfd_decode.cpp` — pybind11 C++ extension that
  replaces `FaceDetector._decode_scrfd()`. Fuses all 6 anchor passes (3 strides × 2
  anchors) into one tight C++ loop; GIL released during compute. Eliminates ~8 400
  Python per-cell operations per frame. Expected savings: 3–5 ms/frame on Pi 5.
- `src/perception/scrfd_decode/setup.py` — build script for the extension (`-O3
  -march=native -ffast-math`).
- `scripts/build_scrfd_decode.sh` — convenience build wrapper (run from repo root).
- `pybind11>=2.12.0` added to `requirements.txt`.
- `FaceDetector._decode_scrfd()` now dispatches to the C++ extension when available;
  falls back to the existing pure-Python path silently if the `.so` is absent.
  Output is bit-exact to the Python path (verified at 9 690 detections, atol=1e-4).

## [1.9.3] - 2026-05-12
### Added
- CLI: new `da quiet-hours` command — `status`, `enable`, `disable`, `set --start HH:MM --end HH:MM`
  routes through the existing `GET/PUT /api/settings/quiet-hours` web endpoints.

### Fixed
- CLI: stale module docstring listed `da version`, `da pan --to`, `da move-servo` as top-level
  commands; updated to show current correct examples (`servo pan`, `servo move`, `system version`).
- CLI: removed dead `_wait_for_spoken_text()` function (57 lines, never called after `da say --wait`
  was removed).
- CLI: removed duplicate `da vision rotation` / `da vision rotation-status` sub-commands which
  shadowed the canonical `da camera rotation` / `da camera rotation-status`; removed now-dead
  `cmd_vision_rotation` and `cmd_vision_rotation_status` handler functions.
- CLI: updated `cmd_help()` `vision` entry — no longer claims "object detection" (that is its own
  command group); now correctly lists only `vision describe`.
- CLI: updated `vision` parser `help=` string to match new scope.

## [1.9.2] - 2026-05-12
### Added
- Web dashboard: new **System Resources** card with live CPU and memory sparkline
  graphs (60-second rolling history). Charts are rendered via Canvas 2D — no
  external library dependency.
- `WebService._build_status_snapshot()` now samples `psutil.cpu_percent()` and
  `psutil.virtual_memory().percent` each WebSocket tick and includes
  `cpu_percent`, `mem_percent`, `cpu_history`, and `mem_history` in the snapshot.
- `psutil>=5.9.0` added to `requirements.txt`.

## [1.9.1] - 2026-05-12
### Changed
- `ObjectDetector._letterbox()`: preallocated reusable 640×640×3 buffer in `__init__`
  (lazy-init fallback for test fixtures using `__new__`), eliminating 1.2 MB heap
  alloc per inference call — mirrors the FaceDetector v1.8.22 change.
- `HeadTracker._clamp()`: replaced `np.clip()` scalar call with `min(max())` —
  avoids numpy dispatch overhead at 20 Hz.
- `FaceRegistry.find_match()`: replaced per-row `np.dot()` scalar loop with a
  cached `(N, 512)` float32 matrix; matching is now one BLAS `matmul` + `argmax`
  call. Cache is invalidated on every write (`register`, `set_name`,
  `add_embedding`, `add_embedding_if_needed`, `merge_faces`, `delete_face`,
  `delete_guest_faces`, `delete_all_faces`).

---

## [1.9.0] - 2026-05-12
### Added
- Dad jokes auto-refresh from icanhazdadjoke.com: fetches 60 jokes per refresh, cached
  to `~/.config/desktop-assistant/dad_jokes.json`, refreshed daily in a daemon thread.
  Falls back to the original 25 hardcoded jokes if offline or cache is empty.
- `ClockAnnouncer` now spawns a dedicated `joke-refresh` daemon thread that fires
  `_refresh_joke_pool()` every hour (gated by the 24h interval check).
### Fixed
- Music audio choppy/underwater: pianobar now launched with `PULSE_LATENCY_MSEC=500`
  giving the PulseAudio client a 500ms buffer, preventing underruns under CPU load.

---

## [1.8.22] - 2026-05-12
### Changed
- **Face detection pipeline speedups (part 2 of 2)** — two more wins on the
  Hailo-8 SCRFD path:
  - **Preallocated letterbox buffer.** `FaceDetector._preprocess` no longer
    allocates a fresh `np.zeros((640, 640, 3), uint8)` per frame. The buffer
    is created once in `__init__` and reused; pad regions are zeroed in-place.
    The static signature is preserved (`buf` is an optional arg) so existing
    tests and any external callers keep working.
  - **Deferred sigmoid in SCRFD decode.** The score-map sigmoid used to run
    across all ~8400 anchor cells per frame just to threshold against
    `conf_thr`. Since `sigmoid` is monotonic, we now threshold in raw logit
    space using `logit(conf_thr)` and only apply sigmoid to the small handful
    of survivor cells. Bit-exact equivalent of the previous behavior.
- **Remaining two optimizations marked blocked**:
  - NMS-baked SCRFD HEF: not present on disk; would require sourcing or
    recompiling via the Hailo Model Zoo.
  - Async Hailo inference pipelining: the `hailo_platform` 4.18 Python API
    does not expose a clean async submit/collect interface; would require a
    significant refactor of `HailoInference` and risks regressing the object
    detector that shares it. Deferred until the API improves or the user
    explicitly requests it.

## [1.8.21] - 2026-05-12
### Fixed
- **Face recognition broken in v1.8.20** — the `cv2.estimateAffinePartial2D`
  method swap from `LMEDS` to `0` produced degenerate affine transforms,
  yielding garbage ArcFace embeddings. Every appearance of a known face was
  treated as a brand-new guest. Reverted to `cv2.LMEDS` and left a comment
  warning future-us not to swap it. The other three v1.8.20 speedups remain.

## [1.8.20] - 2026-05-12
### Changed
- **Face detection pipeline speedups** — four cumulative wins:
  - `PerceptionConfig.max_fps` raised from 2.0 → 10.0. On Hailo-8, SCRFD-10G
    runs well above 10 fps; the previous 2 fps cap was a CPU-Haar holdover.
  - `FaceDetector._decode_scrfd` fully vectorised. Removed the per-detection
    Python loop that built xywh tuples and per-anchor keypoint copies — now
    builds numpy arrays per scale/anchor and concatenates once.
  - `FaceEmbedder._align` switched from `cv2.LMEDS` to plain least-squares
    similarity fit. LMEDS is iterative robust fitting; pointless for exactly
    5 matched landmarks.
  - `PerceptionService` added an embed-skip fast path: if the same face is
    still in roughly the same location within 1 s of the last identification,
    reuse the cached identity instead of re-running ArcFace + registry lookup.

## [1.8.19] - 2026-05-12
### Changed
- **Larger overlay text, scaled with resolution** — increased font scale multipliers
  (relative to 640×480 baseline) across all three overlay draw functions:
  - Face name label: `0.45×scale` → `0.65×scale` (min 0.45)
  - Object detection label: `0.4×scale` → `0.58×scale` (min 0.4)
  - Servo pan indicator: `0.55×scale` → `0.75×scale` (min 0.45)
  - Face/servo label stroke thickness: `round(scale)` → `round(1.5×scale)`
  Text remains proportional to resolution — same physical size in 640×480,
  larger and easier to read at higher resolutions.

## [1.8.18] - 2026-05-12
### Fixed
- **Head hunting during face tracking** — Two-part fix for the servo oscillating
  around a detected face:
  1. **EMA face smoothing** — `HeadTracker._update_tracking()` now applies an
     exponential moving average (`face_ema_alpha=0.25`) to the raw face centroid
     before computing the servo target. This filters per-frame bounding-box jitter
     (±10–30 px) that was the primary source of high-frequency hunting.
     EMA state resets when the face is lost so the next detection starts fresh.
  2. **Tuned control parameters**: `spring_k` 3.5→2.0 (less stiff), `damping`
     3.8→3.2 (still overdamped relative to new spring), `tracking_gain` 0.6→0.35
     (less aggressive per-frame correction), `dead_zone_frac` 0.05→0.08 (wider
     dead band, ~100 px at 1280 wide).
### Added
- `HeadTrackerConfig.face_ema_alpha` field (default 0.25); configurable via
  `head_tracking.face_ema_alpha` in `config/assistant.yaml`.

## [1.8.17] - 2026-05-11
### Removed
- **Cross-camera focus sync** — Removed the `vision.lens_position` publish/subscribe
  pipeline (VisionService → RawCameraService) that mirrored cam0's lens position onto
  cam1. Both cameras are now back to independent continuous AF (AfMode=2). Syncing
  cam0's focus onto cam2 in manual mode was unreliable: cam0 often focuses at infinity,
  locking cam2 there too. Both IMX708 Wide sensors have PDAF and focus well independently.
- Removed `_lens_publish_counter` from VisionService `__init__`.
- Removed `_on_lens_position()` handler and timeout watchdog from RawCameraService.
- Removed `vision.lens_position` bus subscription from RawCameraService.


### Fixed
- **cam2 locking to infinity when cam0 has no close subject** — `RawCameraService`
  now tracks the last time a `vision.lens_position` message was received. If no
  focused position arrives for >3 s (e.g. cam0 is scanning or pointing at a distant
  scene), cam2 reverts to continuous AF so it can independently track nearby subjects.
  Tracked via `_last_sync_ts`, `_sync_active`, and `_FOCUS_SYNC_TIMEOUT=3.0` fields.
  The watchdog runs in `run_tick()`.
### Added
- `Camera.set_continuous_af()` — new method that calls `set_controls({"AfMode": 2,
  "AfSpeed": 1})` to restore continuous autofocus; used by the RawCameraService
  watchdog.

## [1.8.15] - 2026-05-11
### Fixed
- **Focus sync locking cam1 to infinity during AF scan** — `current_lens_position`
  now returns `None` when cam0's AfState=1 (Scanning), so `vision.lens_position`
  is only published when cam0 is actually focused (AfState=2). Previously, the
  transient LensPosition=0.0 captured during the startup AF sweep was immediately
  applied to cam1, freezing it at infinity focus.
- Camera now stores `_af_state` (from ISP metadata) alongside `_lens_position`.


### Added
- **Cross-camera focus sync** — Camera 0 (main VisionService cam) is now the
  autofocus master. Its ISP-reported `LensPosition` (diopters) is read every
  frame via `capture_request()` (replacing the old `capture_array()` call) and
  published on `vision.lens_position` at ~2 Hz. `RawCameraService` subscribes
  and applies the same lens position to Camera 1 via `set_controls({"AfMode": 0,
  "LensPosition": ...})`, keeping both cameras locked to the same focus distance
  without measurable framerate impact.
### Fixed
- `Camera.set_resolution()` now preserves `af_mode` and `lens_position` fields
  in the rebuilt `CameraConfig`.

## [1.8.13] - 2026-05-11
### Fixed
- **Object boxes still persisted after v1.8.12 fix** — the detection worker
  loop was not checking `_enabled` before publishing results, so frames already
  in the queue were processed and repopulated `_latest_objects` *after* the
  clear. Fixed by: (1) checking `_enabled` in `_detection_loop` before
  publishing; (2) draining the frame queue in `_on_set_enabled` when disabling.


### Fixed
- **Object boxes persist after disabling detection** — `VisionService` now
  subscribes to `object.enabled_changed`; when detection is disabled it
  immediately clears `_latest_objects` so stale bounding boxes are removed
  from the next encoded frame.


### Fixed
- **Camera color (red still appeared blue after v1.8.10)** — root cause was a
  libcamera v0.7/PiSP naming quirk: the "RGB888" stream format stores bytes as
  B-G-R in memory (DRM convention), so `capture_array("main")` already returns
  a BGR-ordered array. Removed the erroneous `cv2.cvtColor(RGB→BGR)` that was
  double-swapping channels. Also corrected `face_detector._detect_haar()` to
  use `COLOR_BGR2GRAY` now that the frame byte-order contract is explicit.

## [1.8.10] - 2026-05-11
### Fixed
- **Camera color (red appeared blue)** — changed `stream_format` from `"BGR888"` to
  `"RGB888"` (the camera's native format) and added an explicit
  `cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)` in the capture loop. This makes the
  RGB→BGR conversion unambiguous and independent of libcamera/picamera2 version
  behaviour. Both cameras (cam0 and cam1 via `RawCameraService`) benefit because
  `RawCameraConfig` inherits the same default.


### Added
- **Live pan-slider update** — web GUI head-position slider now tracks the actual
  servo angle in real-time via WebSocket (updated ~0.5 s) during face tracking or
  random motion. Slider is not overwritten while the user is actively dragging.
- **Slider drives head directly** — moving the pan slider now immediately sends a
  debounced (80 ms) `POST /api/pan` without needing to press "Go". The "Go" button
  is retained for precise numeric entry. `pointerdown/up/cancel` events guard against
  auto-updates interfering with drag gestures.
- **Center head on shutdown** — `MotionService.on_stop()` moves the servo to 180°
  (center) before relaxing/stopping. Web GUI reboot/shutdown endpoints publish a
  center command 1.2 s before exec. `da reboot` / `da shutdown` CLI commands also
  call `/api/pan` to center then wait 1.2 s.
- **Resolution-scaled camera overlays** — face ovals, object boxes, labels, and the
  pan-angle arc compass all scale proportionally with `scale = min(w/640, h/480)`.
  At 640×480 (scale=1.0) they are identical to before; at higher resolutions they
  stay the same physical size on screen.


### Added
- **Autofocus mode control for both cameras** — `CameraConfig` and `RawCameraConfig`
  now include `af_mode` (`"continuous"` | `"auto"` | `"manual"` | `"off"`) and
  `lens_position` (diopters, used in manual mode).
- Both cameras default to `af_mode: continuous` (libcamera `AfMode=2, AfSpeed=1`)
  so both CSI feeds continuously track focus and stay in sync.
- Config exposed in `config/assistant.yaml` under `camera.af_mode` and
  `camera2.af_mode` — change to `manual` with a `lens_position` to lock both at a
  fixed desk distance (e.g. `lens_position: 1.0` = 1 metre).


### Added
- **System-level EQ via PipeWire filter-chain** — new `src/audio/pipewire_eq.py`
  module writes a PipeWire filter-chain config (`~/.config/pipewire/filter-chain.conf.d/da-eq.conf`),
  restarts the `filter-chain` user service, and sets the resulting "DA Equalizer" virtual
  sink as the system default. All audio — pianobar, TTS, beeps — routes through it, so
  EQ changes are heard on music for the first time.
- 5-band biquad presets (lowshelf + 3× peaking + highshelf) for all named presets:
  `flat`, `bass_boost`, `treble_boost`, `vocal`, `loudness`, `warm`.
- Custom EQ bands from the web UI are converted to PipeWire peaking filters.
- On daemon restart, `AVService` calls `ensure_default()` to re-elect the EQ sink
  without restarting filter-chain (config persisted from last session).
- When PipeWire EQ is active, the Python software biquad path is set to `flat` to
  avoid double-processing TTS audio.


### Fixed
- **Vision service no longer shows red at startup** — `camera.is_ready` property added
  to `Camera`; returns `True` only after the capture thread deposits the first frame.
  `VisionService.run_tick()` silently skips ticks until the camera is ready, preventing
  spurious `vision.error` events during the startup initialization race.
- **Services auto-recover from error state** — `WebService` and `IPCBridge` now
  subscribe to healthy-signal events (`vision.jpeg_ready` → `vision`,
  `audio.chunk` → `audio_capture`) and clear the `error` state back to `running`
  when the service resumes normal operation.


### Fixed
- **EQ preset now persists across daemon restarts** — `MusicService.set_eq_preset()`
  writes the selected preset to `~/.config/desktop-assistant/music_eq_preset.txt` and
  restores it on `on_start()`. The `"custom"` preset is also persisted when the user
  applies custom EQ bands via the web API. The custom EQ panel is now shown
  automatically on page load when the persisted preset is `"custom"`.
- **Object detection enabled/disabled now persists** — `ObjectService._on_set_enabled()`
  writes `true`/`false` to `~/.config/desktop-assistant/object_detection_enabled.txt`
  and restores the state on `on_start()`, overriding the config default.

## [1.8.4] - 2026-05-11
### Added
- **Service error (red) state in Services panel** — `WebService` now subscribes to
  `audio.error`, `vision.error`, `perception.error`, `music.error`, and `thermal.error`.
  Any service that fires an error event while running transitions to `"error"` state and
  renders as a red ⚠ pill in the web GUI. Clears back to green when the service restarts.
- **`da status` colorized service list** — services now display green ✓ (running), yellow ✗
  (stopped), or red ⚠ (running but degraded). `IPCBridge` tracks the same error events and
  propagates `error: True` in its `_service_status` dict for the CLI to read.

## [1.8.3] - 2026-05-11
### Fixed
- **Services panel now shows all services** — `WebService` and `IPCBridge` both start at
  the end of the service list, so all earlier services had already published their
  `service.started` events before either one was subscribed. Both now seed their status
  registry at `on_start()` time by iterating the full services list and calling
  `is_running()` on each peer. Expected services in the panel: `motion`, `vision`,
  `audio_capture`, `av`, `perception`, `object`, `telemetry`, `clock`, `face`, `music`,
  `raw_camera2` (if cam2 enabled), `tracking`, `ipc_bridge`, `web`.
- `core_main.py`: After building the full `services` list, sets `_all_services` on both
  `ipc` and `web_svc` so they receive the reference before `run_services()` starts.


### Fixed
- **Restore top-level `da ping`** — `ping` was moved exclusively under `system ping`
  in v1.8.1, breaking the commonly used `da ping` health check shortcut. Added it back
  as a top-level alias (both `da ping` and `da system ping` now work).


### Fixed
- **CLI fully restored** — the `scripts/desktop-assistant` script had regressed to ~630
  lines and was missing all structured subcommand groups added at v1.7.0. Full command
  set is restored and all new commands since v1.7.0 are included:
  - Restored: `system`, `face`, `vision`, `music`, `servo`, `face-tracking`,
    `random-motion` groups with all their sub-commands
  - Restored: `say`, `joke`, `time`, `watch`, `topics`, `publish`, `last`, `status`
    top-level commands
  - Added: `camera rotation` and `camera rotation-status` under the `camera` group
  - Added: `camera2 rotation` and `camera2 rotation-status` under new `camera2` group
  - Added: `eq set` and `eq custom` under new `eq` group (voice/TTS EQ)
  - Added: `object-detection enable|disable|status` (carried from v1.8.0)
  - Added: `_publish()` helper (was referenced but never defined; caused error in
    `face greeting-cooldown`)
  - `cmd_help()` updated to exactly match all registered subparsers

## [1.8.0] - 2026-05-11
### Added
- **Object detection toggle** — enable/disable COCO object classification at runtime:
  - `ObjectService` gains `_enabled` flag and `object.set_enabled` bus handler;
    publishes `object.enabled_changed` on state change
  - `ObjectConfig.enabled` field (default `true`) read from `config/assistant.yaml`
    under new `object_detection.enabled` key
  - Web dashboard: checkbox toggle in the Settings → System section
  - CLI: `da object-detection enable|disable|status`
  - `WebService` exposes `GET/PUT /api/settings/object-detection`


### Fixed
- **FPS overlays not showing in Chrome** — Chrome does not fire `img load` events
  for each MJPEG frame after the initial load. Replaced the client-side `img.onload`
  counter with server-side FPS tracking: `WebService._on_frame`/`_on_frame2` increment
  per-camera counters; `_build_status_snapshot` computes fps from elapsed time and
  resets counters each call. `cam1_fps` and `cam2_fps` are now included in the
  WebSocket status push; the browser reads them in `updateDashboard()` →
  `updateFpsOverlays()`.


### Changed
- **VisionService: async JPEG encoder thread** — decouples `frame.copy()`,
  `_draw_overlays()`, and `cv2.imencode()` from the capture tick. The fast tick
  path (~2ms) now only does `capture_frame()` + rotation + publishes
  `vision.frame_ready` (for Hailo detection). A dedicated background encoder
  thread picks up frames from a `Queue(maxsize=1)`, performs the expensive
  copy/draw/encode work, stores the JPEG, then publishes `vision.jpeg_ready`.
  Cam1 MJPEG stream in WebService now subscribes to `vision.jpeg_ready` instead
  of `vision.frame_ready`, eliminating encoder stalls from the detection loop.
- **Expected FPS improvement**: tick at near ISP rate (~30fps); MJPEG stream
  limited by encoder thread throughput (~20fps) rather than detection latency.

## [1.7.18] - 2026-05-11
### Changed
- **Camera: restored background capture thread (v1.7.16 design)** — the no-background-thread
  approach (v1.7.17) suffered severe GIL contention from Hailo inference, causing
  `capture_array()` to block 77–169ms instead of ~33ms. Restored background thread
  that decouples ISP frame delivery from GIL pressure; DMA→heap copy happens in
  `capture_frame()` inside the lock (prevents concurrent DMA access). Added comments
  explaining the design rationale.
- **VisionService: finer timing log** — split "overlays" into "copy", "draw", and "encode"
  phases for future profiling.


### Changed
- **Camera: removed background capture thread** — reverted to direct `capture_array()`
  in the service tick. The background-thread approach caused concurrent DMA/heap memory
  operations that inflated `frame.copy()` from ~5ms to 89ms. With `NoiseReductionMode=0`
  already in place, `capture_array()` blocks ~33ms (one ISP frame interval), making
  a background thread unnecessary. `Camera` class is now simpler: no `_capture_loop`,
  no `_frame_lock`, no `_latest_array`. `capture_frame()` calls `capture_array()` directly
  and returns `arr.copy()` (immediate heap copy from DMA buffer).
- **VisionService timing** — added finer `copy`/`draw`/`encode` split in timing log to
  distinguish frame copy from overlay drawing for future profiling.

## [1.7.16] - 2026-05-11
### Changed
- **Camera: background capture thread** — `Camera.start()` now spawns a dedicated
  `cam{N}-capture` daemon thread that continuously calls `capture_array("main")` and
  stores the latest frame. `capture_frame()` is now non-blocking (copies from buffer),
  decoupling service tick rate from ISP pipeline latency.
- **NoiseReductionMode: Off** — added `NoiseReductionMode: 0` to picamera2 controls in
  `Camera.start()`. Eliminates per-frame ISP noise-reduction processing, the primary
  cause of ~4fps throughput at 640×480.
- **BGR888 format** — `CameraConfig.stream_format` default changed from "RGB888" to
  "BGR888". Frames are now native cv2 byte order; eliminates the latent R↔B color swap
  in JPEG output and removes any implicit conversion overhead.
- **Servo overlay optimized** — `_draw_servo_overlay` no longer does a full-frame
  `frame.copy()` + `cv2.addWeighted`. Now uses an in-place ROI right-shift (`>> 1`)
  on only the ~80×20 label background region — ~7000× less data touched per frame.
- **`capture_still` properly stops background thread** — now calls `self.stop()` /
  `self.start()` instead of directly manipulating `_cam`, preventing a race condition
  with the background capture thread during still captures.


### Changed
- MJPEG stream generators now use `asyncio.Event` + `loop.call_soon_threadsafe()`
  instead of `run_in_executor(threading.Event.wait)` — eliminates thread-pool
  dispatch overhead (one task per frame) for lower latency and less CPU churn.
- `VisionService.on_start()` now sets `tick_seconds = 1/framerate` from the
  camera config instead of the hardcoded 0.033 class constant.
- `picamera2` `buffer_count` increased from 4 → 6 for more ISP pipeline headroom.
- Camera 2 default framerate raised from 15 → 30 fps (`config/assistant.yaml`
  and `RawCameraConfig` default); Pi 5 handles dual 30fps without issue.

## [1.7.14] - 2026-05-11
### Fixed
- Camera 2 rotation is now persisted across daemon restarts.
  - `core_main.py` reads `_rt["camera2"]["rotation_deg"]` at startup (was reading YAML only).
  - Subscribes to `camera2.rotation_changed` and saves to runtime state JSON.
  - `_rt_state` now includes a `"camera2"` section.

## [1.7.13] - 2026-05-11
### Added
- Live FPS counter overlaid on each camera feed (bottom-left corner, yellow badge).
  Counts MJPEG frame load events per second client-side — no server changes needed.

## [1.7.12] - 2026-05-11
### Fixed
- Camera feeds now always display side-by-side (`flex-wrap: nowrap`; scrolls
  horizontally on very narrow viewports instead of stacking vertically).
- Both feeds rendered in an identical `aspect-ratio: 4/3` box so they appear
  the same size regardless of stream resolution; letterboxed if aspect differs.

 - 2026-05-11
### Added
- **Camera resolution adjustment** — both cameras share a single resolution setting.
  Publishing `camera.set_resolution` `{"width": int, "height": int}` on the bus changes
  both `VisionService` (Camera 1) and `RawCameraService` (Camera 2) simultaneously.
- `Camera.set_resolution(width, height)` method in `src/vision/camera.py`; stops and
  restarts the stream only when running on real hardware, no-op in sim mode.
- `Camera.resolution` property returning `(width, height)` tuple.
- `VisionService.resolution` property; publishes `camera.resolution_changed` after change.
- `RawCameraService.resolution` property; updates stored `RawCameraConfig` on change.
- Resolution persisted in `~/.config/desktop-assistant/runtime_state.json` under
  `camera.width` / `camera.height`; restored at startup for both cameras.
- `core_main.py` subscribes `camera.resolution_changed` → persists state + updates
  tracker `frame_width` for continued accurate head-tracking after resolution change.
- `GET /api/settings/camera/resolution` — returns current resolution (both cameras).
- `PUT /api/settings/camera/resolution` — sets resolution via bus publish.
- Web UI: Resolution dropdown (320×240, 640×480, 1280×720, 1920×1080) in the Controls
  section, with "Applied ✓" feedback and interruption warning.
- CLI: `da camera resolution WxH` subcommand to set resolution from the terminal.

## [1.7.10] - 2026-05-11
### Removed
- `tests/test_servo_controller.py` — servo unit tests were commanding real hardware
  during regression runs and interfering with live servo positioning.


### Added
- **Camera 2 live rotation** — `RawCameraService` now supports live rotation via
  `camera2.set_rotation` bus topic; persists under lock. `rotation_deg` property
  exposed for API reads.
- `GET/PUT /api/settings/camera2/rotation` REST endpoints in WebService.
- Web UI: Cam 2 rotation slider + preset selector (parallel to Cam 1 control).
- **Custom EQ bands** — `AudioOutputConfig` gains `custom_eq_bands`; `AudioOutput`
  adds `set_custom_eq_bands()` and `_build_custom_sos()` using peaking biquad filters.
- `AVService` subscribes to `av.set_eq_preset` and `av.set_custom_eq`; restores EQ
  state from `~/.config/desktop-assistant/eq_preset.txt` / `custom_eq.json` on startup.
- `GET/PUT /api/music/eq/custom` REST endpoints.
- Web UI: Custom EQ panel (hidden until "Custom…" preset selected) with per-band
  Hz / gain / Q sliders, add/remove bands, Apply button.
- `da camera2 rotation <degrees>` CLI command.
- `da eq set <preset>` and `da eq custom <band-specs...>` CLI commands.
- Added `"custom"` to `MusicService.EQ_PRESETS` list.

## [1.7.8] - 2026-05-11
### Added
- `da face` CLI subcommand group: `meet <name>`, `list`, `forget-all`,
  `forget-guests`, `greeting-cooldown <minutes>`, `greeting-settings`
- `FaceService._on_set_cooldown` now accepts and applies all greeting settings
  fields (`jitter_pct`, `min_absence_s`, `confidence_threshold`) in addition to
  `cooldown_min`
- `api_put_greeting` web endpoint now broadcasts all updated fields to the live
  daemon (previously only `cooldown_min` was forwarded)
### Fixed
- Restored orphaned `cmd_say` function body that had been left as floating code
  outside any function definition in `scripts/desktop-assistant`
- Removed unreachable code fragment after `return True` in
  `FaceRegistry.merge_faces()`

---

## [1.7.7] - 2026-05-11
### Added
- Second CSI camera support: new `RawCameraService` captures from camera index 1
  at 15 fps (configurable), encodes JPEG, and publishes `vision.frame2_ready`
- New `/stream2` MJPEG endpoint in `WebService` served from the second camera
- Web GUI now shows both feeds side by side with labels ("Cam 1 — Face Tracking",
  "Cam 2 — Raw"); Cam 2 section auto-hides if the stream is unavailable
- `camera2` section added to `config/assistant.yaml` (enabled, index, width,
  height, framerate, rotation_deg); set `enabled: false` to disable entirely

---

## [0.8.18] - 2026-05-04
### Changed
- `desktop-assistant move-servo -as` now waits for the start speech to
  complete (observed via `av.spoke`) before publishing `motion.pan_to`.
- `desktop-assistant move-servo -af` now announces that the move has
  stopped only after the move request has completed.

### Fixed
- If `-as` is set and speech completion is not observed in time, the CLI now
  exits with `{"ok": false, "error": "start_announcement_timeout"}` and does
  not start servo motion.

---

## [0.8.23] - 2026-05-04
### Added
- `TextToSpeech.render_duration(text) -> float` — returns exact playback
  duration in seconds by running the full Piper render pipeline (no audio
  output). Falls back to a word/char heuristic in sim mode.
- `AVService.tts_duration_rpc(text)` — thread-safe wrapper that lazy-
  initialises TTS if the service hasn't started yet.
- `IPCBridge.register_rpc(cmd, fn)` — lets services attach custom REP
  command handlers without editing the bridge directly.
- New REP command `tts_duration` wired in `core_main` so the CLI can
  query the exact duration of any phrase.

### Changed
- `desktop-assistant move-servo -as` now queries `tts_duration` over the
  REP socket for the exact rendered speech duration before computing the
  motion lead time, replacing the word/char heuristic. Falls back to the
  heuristic if the daemon is unreachable.

---

## [0.8.22] - 2026-05-04
### Changed
- `desktop-assistant move-servo -as` now starts motion slightly before the
  end of the start announcement instead of waiting for full `av.spoke`
  completion. The CLI uses a conservative speech-duration estimate and a
  220 ms overlap window to reduce perceived lag.

---

## [0.8.21] - 2026-05-04
### Changed
- `desktop-assistant move-servo -as` now waits on the live `av.spoke` PUB
  event stream instead of polling `last av.spoke` over REP, reducing the
  gap between the start announcement finishing and motion beginning.

### Fixed
- Kept the older `last av.spoke` fallback for compatibility if PUB/SUB
  delivery misses due to ZeroMQ slow-joiner timing.

---

## [0.8.20] - 2026-05-04
### Changed
- `av.say` now accepts optional `request_id`, and `av.spoke` echoes it,
  allowing the CLI to wait for the exact announcement instance to finish.
- `desktop-assistant move-servo -as` now waits on that correlation before
  issuing motion, so movement starts only after start speech completes.

### Fixed
- Added backward-compatible fallback when older running AV services do not
  emit correlated `av.spoke` payloads, preventing false
  `start_announcement_timeout` failures.

---

## [0.8.19] - 2026-05-04
### Changed
- `desktop-assistant move-servo -as` now waits for a new `av.spoke` event
  before issuing `motion.pan_to`, ensuring the start announcement finishes
  before movement begins.
- `av.spoke` payload now includes `ts` to allow reliable detection of new
  speech events from the CLI.

### Fixed
- Start-announcement waiting no longer falsely succeeds on stale `av.spoke`
  payloads from earlier commands.

---

## [0.8.17] - 2026-05-04
### Fixed
- `desktop-assistant move-servo` and `desktop-assistant pan --move-time-ms`
  now use a request timeout derived from move duration (`move_time_ms + 3s`
  floor at 2s), preventing false CLI timeouts on valid longer moves.

---

## [0.8.16] - 2026-05-04
### Added
- `desktop-assistant move-servo` now supports `-as` / `--announce-start` to
  speak when the servo move request starts.
- `desktop-assistant move-servo` now supports `-af` / `--announce-finish` to
  speak after a successful move request is accepted.

---

## [0.8.15] - 2026-05-04
### Added
- New CLI command `desktop-assistant move-servo <position> <move_time_ms>` to
  request a servo move with explicit target position and travel time.

### Changed
- `motion.pan_to` now accepts optional `move_time_ms` and converts it to
  `speed_deg_per_sec` for `ServoController.move_to(...)`.
- Existing `desktop-assistant pan --to ...` command now also accepts optional
  `--move-time-ms`.

### Fixed
- Motion service now validates `move_time_ms` and ignores invalid values
  (non-numeric or <= 0) rather than attempting an unsafe move call.

---

## [0.8.14] - 2026-05-02
### Changed
- Default TTS voice changed to **en_US-lessac-high** with TNG-computer
  tuning (`length_scale=1.15`, `noise_scale=0.3`, `noise_w=0.5`).
  Delivery is now measured, flat, and authoritative.

---

## [0.8.13] - 2026-05-01
### Changed
- Replaced espeak-ng TTS backend with **Piper** neural TTS (en_US-amy-medium
  voice, 22 kHz). Speech now sounds natural and modern rather than robotic.
  Voice model lives at `config/piper/en_US-amy-medium.onnx`. espeak-ng is
  retained as an automatic fallback if the model is absent.
- `TTSConfig` gains Piper-specific fields: `piper_voice_name`, `piper_model`,
  `piper_length_scale`, `piper_noise_scale`, `piper_noise_w`.
- `say()` with no `output` argument now plays via sounddevice instead of
  spawning espeak subprocess directly (consistent code path).
- Updated TTS unit tests to reflect new backend-detection contract.

---

## [0.8.12] - 2026-05-01

### Fixed
- AVService now serializes all audio output through a single-threaded
  worker queue. Previously the boot self-test chime, fired ~3 s after
  startup, would cut into the still-playing version announcement
  because both ran on independent threads sharing the DAC. Now
  `say` / `chime` / `beep` / `announce_version` events all queue and
  play strictly in order. Added `AVService.wait_idle()` for callers
  (and tests) that need to know the queue has drained.

---

## [0.8.11] - 2026-05-01

### Changed
- Two more software loudness wins for the unamplified bring-up speaker:
  - `AudioOutput.play()` now applies a `tanh()` soft-clipping
    waveshaper (drive default 3.0) to every output. Pushes RMS up
    while keeping peaks bounded at -0.4 dBFS — adds ~5-6 dB perceived
    loudness on speech, ~3 dB on tones, with mild harmonic distortion.
    Configurable via `AudioOutputConfig.loudness_boost` (1.0 disables).
  - Boot chime moved up an octave: A5/C#6/E6 (880/1109/1319 Hz)
    instead of C5/E5/G5. Sits in the small-speaker resonance band
    (1-3 kHz) and the ear's most sensitive band (Fletcher-Munson),
    so it's audibly louder on the same DAC level.

### Notes
- ALSA `Speaker Playback Volume` on the CM108 is at 36/37 (≈ -1 dB),
  so all remaining mixer headroom is spoken for. Real loudness fix
  remains the PAM8403 amplifier.

---

## [0.8.10] - 2026-05-01

### Fixed
- `AudioInput.__init__()` now probes the chosen input device with
  `sd.check_input_settings()` and falls back to sim mode if the probe
  fails. Without this, the unwired mic on the CM108 USB DAC was
  causing PortAudio to `SIGABRT` the whole core process every ~3 s,
  triggering systemd restart loops. With this, the boot self-test
  reports the mic as offline cleanly and core stays up.

---

## [0.8.9] - 2026-05-01

### Fixed
- `AVService.on_start()` now runs `announce_startup()` on a daemon
  thread instead of blocking the main service-startup thread. With
  v0.8.8's `audio_output=` fix, the synchronous TTS playback was
  blocking on `sd.wait()` (which never returned because the parallel
  `AudioCaptureService` was hammering PortAudio with input-stream
  errors). Result: `ipc_bridge` never started and the CLI timed out
  for the entire process lifetime.
- `AudioCaptureService.run_tick()` now backs off after 3 consecutive
  `mic.record` failures and stops trying until the service restarts.
  Without a mic wired, the previous behaviour was to keep opening &
  closing failing PortAudio input streams 4×/s, which can wedge the
  shared USB-DAC output stream.

---

## [0.8.8] - 2026-05-01

### Fixed
- `AVService.on_start()` crashed building `VersionAnnouncer(output=...)`
  with `TypeError: unexpected keyword argument 'output'` (correct kwarg
  is `audio_output`). Result: the boot startup announcement never ran
  and the boot self-test reported the AV service as unhealthy, firing
  the *failure* chime + "Boot self test failed" speech on every boot.

---

## [0.8.7] - 2026-04-29

### Changed
- Pumped software audio gain for unamplified bring-up:
  - `AudioOutput.chime()` default amplitude 0.25 → 0.9.
  - `TextToSpeechConfig.amplitude` default 100 → 200 (espeak-ng max).
  - `TextToSpeech._render_to_array()` now peak-normalizes WAV output to
    -1 dBFS before handing it to `AudioOutput.play()`.
- ALSA mixer `Speaker` is already at 97% on the CM108 USB DAC, so the
  remaining volume increase will come from the PAM8403 amplifier when
  installed.

---

## [0.8.6] - 2026-04-29

### Added
- Boot startup chime: `AudioOutput.chime()` plays a short C5-E5-G5
  ascending arpeggio (with 5 ms attack/release envelopes to avoid clicks).
  Mono signal is duplicated to both channels so it's audible with only
  one speaker wired.
- New bus topic `av.chime` (and reply `av.chimed`) — `AVService` plays
  the chime via `AudioOutput.chime()`. Optional payload overrides
  `notes`, `note_duration`, `gap`, `amplitude`.
- `runner._run_boot_self_test()` now publishes `av.chime` from the core
  process before the spoken summary. A success uses the C-major triad;
  a failure uses a descending E5-C5-G4 motif before the failure speech.
- `scripts/test_chime.py` — manual test script that plays the chime
  then speaks the version, useful during bring-up.

### Fixed
- `AudioOutput.play()` now linearly resamples to the device's configured
  rate before handing off to PortAudio. The CM108-based USB DACs (incl.
  the Unitek Y-247A) refuse non-48 kHz input, which crashed
  `TextToSpeech.say(output=...)` with `Invalid sample rate [-9997]`
  (espeak-ng emits 22050 Hz WAV).

---

## [0.8.5] - 2026-04-28

### Changed
- `AudioOutput` now matches USB audio adapters by ALSA-name substring
  list instead of a single hardcoded "Sabrent". Default needles:
  `("USB Audio", "C-Media", "Sabrent")`, which covers Sabrent
  AU-MMSA/AU-EMAC, Unitek Y-247A (C-Media CM108), and any other generic
  USB DAC the kernel labels "USB Audio Device".
- `AudioOutputConfig.device_name` (singular) is preserved for back-compat;
  when set it becomes the sole match needle.
- `find_output_device()` accepts a string or a sequence of strings.
- `scripts/test_speaker.py` and `scripts/test_tts.py` updated to enumerate
  any USB DAC, not just Sabrent.
- `hardware/audio/audio_notes.md` documents both adapters and the new
  match strategy.

---

## [0.8.4] - 2026-04-27

### Fixed
- `desktop-assistant status` showed `vision  frame#?  ?x?` even when
  the camera was healthy. The CLI formatter was looking for
  `frame_id`/`width`/`height` keys, but `VisionService` publishes
  `index` and `shape: [h, w, 3]`. Formatter now handles both.

---

## [0.8.3] - 2026-04-27

### Fixed
- `desktop-assistant status` now sees thermal telemetry. Thermal runs in
  its own systemd unit with its own in-process `MessageBus`, so events
  it published (`thermal.temp`, `thermal.fan`, `thermal.rpm`) never
  reached the core process — the CLI showed `temp —`.

### Added
- `IPCBridge` accepts an `upstream_endpoints=[...]` list (or env var
  `DA_BUS_UPSTREAM_ENDPOINTS`, comma-separated) and SUBscribes to those
  ZMQ PUB sockets, re-emitting incoming events onto the local bus.
  Loop-safe: events injected from upstream are not re-forwarded back
  out on the local PUB.
- The thermal process now runs its own `IPCBridge` on
  `ipc:///tmp/desktop-assistant-thermal.{pub,rep}` so its bus is visible
  cross-process.
- `core_main` connects upstream to the thermal bridge — the CLI now
  shows live temperature, fan duty, and (once wired) tach RPM.
- Test `test_ipc_bridge_forwards_upstream_to_local_bus` covers the
  cross-process path. 145/145 tests pass.

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
