# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
