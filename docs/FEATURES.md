# VERA Feature Parity Matrix

This document tracks which features are accessible via each interface:
**CLI** (`vera` / `da`), **Web GUI** (`http://localhost:8080`), and
**OpenClaw** (Telegram natural-language skill).

✅ = Available  ❌ = Not available  🔧 = Partial / limited

---

## Web control workspaces

The dashboard uses an original LCARS-inspired visual system: black backgrounds,
orange/peach action surfaces, readable mixed-case content, and continuous rounded
elbows. Each workspace has a distinct navigation color; the selected workspace
colors the main frame and section bars. Section vertical bars are 24px wide
(20px on mobile), with circular inner arcs.
The left sidebar is an LCARS elbow frame: the VERA brand sits above a curved
frame whose spine follows the selected workspace color, a readout shows a live
stardate (year and day of year), and square icon buttons each have their own
spine block, with a white inset bar marking the active workspace. On phones the
sidebar collapses to a sticky three-column button grid.
The title header uses the same frame language: a workspace-colored left bar
sweeps into the bottom band, which ends in a rounded cap.
Sparse number clusters decorate the rail readout and framing, are hidden from
assistive technology, and do not represent live readings. Only the title-frame
numbers refresh when switching workspaces; reselecting the active workspace
leaves them unchanged. Labels use the Barlow Condensed web font from Google
Fonts; no templates, sounds, or number animations are required.
The title spells out VERA as "Vision-Enabled Reasoning Agent" below the name.

| Workspace | Controls |
|-----------|----------|
| Overview | Telemetry, room status, last spoken text, resources/processes, service health, quick-access links |
| Vision & Motion | Both camera feeds, snapshots, full-width Head movement with side-by-side pan/tilt sliders, scene services, tuning, depth, face registry, camera geometry, privacy |
| Media & Audio | Pandora, podcasts, output volume/mute, voice output level, EQ preset, collapsible custom voice EQ |
| Voice & Behavior | Speak/record/play clips, microphone input/analyzer, recognition/wake settings, greetings, quiet hours, skills, transcript history |
| Smart Home | Connected IoT devices and device management |
| System | Updates, fan settings, collapsible audio hardware and microphone DSP, events, power, help |
| Settings | Head Movement Settings: servo enablement, face tracking, random idle motion, pan/tilt travel limits, PCA9685 channels |

Head movement sliders command immediately and follow live position updates when
not being adjusted. Tilt controls and tilt travel settings appear only on bodies
with tilt hardware. Configuration changes retain their existing save behavior.

Daily playback never requires opening hardware configuration. A direct link from
Media & Audio opens System's audio setup when device routing needs attention.
Quick-access links focus and scroll to their destination, revealing advanced
settings when needed. Tabs support arrow keys, Home/End, visible keyboard focus,
and a skip-to-workspace link. Narrow displays use a wrapping navigation grid and
single-column cards; motion-reduction preferences are respected.

The old Audio tab preference migrates to Media & Audio. Existing per-tab card
orders are retained in browser storage but not applied to the new task layout;
subsequent dragging saves a new, workspace-specific order.

Design references (principles/palette only; no template code or artwork copied):
[LCARS Manifesto](http://www.lcars-terminal.de/tutorial/manifesto.htm),
[Creating a Coherent LCARS Interface](http://www.lcars-terminal.de/tutorial/guideline.htm),
and [TheLCARS classic theme palette](https://www.thelcars.com/assets/classic-2702.css).

---

## Vision & Tracking

### Camera focus

In **Vision & Motion > Camera configuration**, each camera has independent
continuous autofocus, single-scan autofocus, and manual focus controls. Select
**Manual focus** to enable the diopter input, then press **Set**. Zero means
infinity; 1 diopter is approximately 1 metre and 2 diopters is 0.5 metres.
Changes apply without restarting capture and are saved in the device-local
`config/runtime_state.yaml`, overriding the corresponding `af_mode` and
`lens_position` defaults under `camera` or `camera2` in `config/assistant.yaml`.
For example:

```yaml
camera:
  af_mode: manual
  lens_position: 0.0
camera2:
  af_mode: continuous
  lens_position: 0.0
```

Focus controls are disabled when the camera is missing or does not advertise
autofocus and lens-position controls. The input uses the camera driver's reported
range; tuning can clamp physical lens travel further. Applying a position does
not guarantee optical sharpness. Enabling autofocus retains the manual setting
for reuse.

`GET /api/settings/camera/focus` and `GET /api/settings/camera2/focus` return
capability, configured mode/position, latest reported lens position, and control
limits. The matching `PUT` endpoints accept `{"af_mode":"manual","lens_position":0.0}`.
Modes are `continuous`, `auto` (single scan), `manual`, and the legacy `off` alias.
Invalid values return HTTP 422; unavailable hardware, RPC failures, and persistence
errors return HTTP 503 rather than reporting success.

### Body profiles

The machine-local `config/body.yaml` selects the body independently of software
version, hostname, or attached peripherals:

```yaml
body: original
```

Use `body: new` on the new body. The file is git-ignored, survives updates,
and accepts optional boolean direction overrides for the individual mount:

```yaml
body: new
head_tracking:
  invert_tilt: true
```

`invert_pan` is also supported. Omitted directions retain the shared profile's
defaults. Restart the core service to apply changes. `invert_tilt: true`
declares a reversed tilt servo mount: the tilt output is mirrored at the servo
so tilt angles always mean 90° level, above 90° up, below 90° down. `invert_pan`
controls the uncalibrated pan model. A measured tracking calibration records its
own direction and is discarded automatically when either direction changes.

The identity file must remain with its hardware. Missing identity keeps new-body behavior
for backward compatibility and logs a warning; an invalid identity is an
explicit startup error. Restart `desktop-assistant-core.service` after changing
identity. No servo probing or hardware guesses are used to select the body.

The versioned `config/body_profiles.yaml` overrides the corresponding
`servo` and `head_tracking` defaults in `config/assistant.yaml`:

| Body | Tracker | Pan hardware | Tilt |
|------|---------|--------------|------|
| `original` | Legacy Kalman/minimum-jerk | PCA9685 channel 15, geared logical 1-360 degrees, soft limits 135-215 degrees, non-inverted pan | Disabled |
| `new` | Person-like gaze (Kalman bearing + minimum-jerk) | PCA9685 channel 1, direct-drive 0-180 degrees, soft limits 70-110 degrees, inverted pan | Channel 0, limits 70-110 degrees |

`head_tracking.controller` may be `legacy`, `gaze`, or `visual_servo`; the
profile chooses it, not the local identity file.

Both profiles enforce safe 500-2500 microsecond pulses; the original profile
does **not** restore the old unsafe 10-2950 microsecond calibration.
Both controllers prefer recognized people, then the largest face bounding box.
Legacy tracking does not require the visual controller's multi-frame
acquisition gate, feeds the existing Kalman/minimum-
jerk controller at 20 Hz, and falls back to fresh person detections or optional
idle motion. After the last tracked face disappears, the head holds for one
second before person seek or idle motion can take over. Face/person tracking
still works during quiet hours; random idle motion does not override silence.

Runtime servo limits/channels are reused when the saved `body` and profile
`revision` match the selected identity; bumping a profile's `revision`
discards stale saved geometry after a wiring correction. Untagged state remains compatible with the new body;
for the original body or a body change, discard mismatched servo geometry but
preserve enable switches, tracking toggles, and camera settings.
Subsequent runtime saves include body identity. Dashboard tuning saves to the
local identity file's `head_tracking` section, not the shared body profiles.
The tuning panel and `tracking.get_status` / `tracking.get_tunable_params` RPCs
report the selected body/controller. Visual calibration is only used by the
`visual_servo` controller.

### New-body gaze tracking (default)

The `gaze` controller aims for smooth, person-like head motion rather than
stop-and-go corrections:

* **World-frame Kalman filter.** Each fresh, locked face (or person) detection
  becomes an absolute bearing: the measured head angle at capture time
  (interpolated from `motion.position`, minus `servo_lag_s`) plus the target's
  angular offset in the image. A constant-velocity Kalman filter smooths that
  bearing per axis. Because the bearing does not depend on where the head has
  moved since, a late or repeated frame cannot compound into overshoot.
* **Saccadic suppression.** Frames captured while the head is moving much faster
  than the target are ignored, as people do during a quick glance, and the
  measurement noise grows with head speed (`timing_uncertainty_s`).
* **Minimum-jerk motion.** Re-fixations follow a bell-shaped velocity profile
  whose duration grows with distance (`move_base_s` + `move_scale_s_per_deg`,
  capped by `move_max_s` and `max_speed_deg_s`). Replans start from the current
  position, velocity, and acceleration, so direction changes stay smooth with no
  jolt.
* **Coordinated pan and tilt.** Both axes share one move duration based on the
  combined angular distance, so diagonal glances travel in a straight line and
  arrive together instead of tracing an L.
* **Undershooting saccades.** Re-fixations larger than `saccade_min_deg`
  deliberately land at `saccade_gain` (90%) of the distance, as human saccades
  do, and a small correction follows if needed. Large moves therefore never
  swing past the face.
* **Smooth pursuit.** When the filtered target speed exceeds
  `pursuit_min_deg_s` and `pursuit_confidence` standard deviations of the
  filter's velocity uncertainty, moves aim where the target will be on arrival
  and end at its velocity (up to `max_pursuit_deg_s`), so the head glides
  alongside a walking person. Large errors during pursuit get a velocity-matched
  catch-up move. Pursuit is blocked for `pursuit_settle_s` after a saccade,
  while the velocity estimate is still disturbed by the head's own motion.
* **Fixation hold with elliptical zones.** After the target has stayed inside
  the settle ellipse (`settle_pan_deg` 3 x `settle_tilt_deg` 2 degrees) for
  `fixation_dwell_s`, the fixation locks on both axes. The head then rests until
  the target leaves the wider release ellipse (`release_pan_deg` 6 x
  `release_tilt_deg` 4 degrees), instead of chasing detector jitter. The gap
  between the two zones is hysteresis.
* **Pre-speech gesture.** When `head_tracking.pre_speech` is configured, speech
  audio starts `lead_s` (0.25 s) after `av.speaking_started`. During that lead
  the head lifts slightly (`lift_deg`, 2 degrees) with a small sway timed to the
  first syllable, then relaxes over `relax_s`, as people do just before talking.
  The gesture is applied only while tracking and stays within the tilt limits.
  It plays only on the first utterance after `cooldown_s` (60 s) of silence.
  Follow-up sentences start without the lead or the lift.
* **Measured field of view.** Image offsets convert to degrees using the
  camera's real field of view. The new body's IMX708 standard lens measures
  about 72 degrees horizontal and 49 degrees vertical (`fov_degrees`,
  `vertical_fov_degrees`). The old 100-degree assumption overstated every offset
  by about 1.6x, which caused overshoot and twitching near center.

Setpoints stream to MotionService at 20 Hz on pan and tilt (tilt within ±20
degrees of its midpoint). Faces take priority; fresh person detections drive the
same controller when no face has been seen for one second. When the target is
lost the filter coasts for up to `max_predict_s`, then the head decelerates to a
natural stop before idle motion may resume. Speaking nods are added on top.
The Kalman filter is the high-rate estimator: it predicts at the 20 Hz control
rate and is corrected whenever a detection arrives (about 7 Hz). The
`/ws/tracking-debug` stream reports the achieved `detect_hz` and capture-to-arrival
`detect_latency_ms`. Stereo face depth runs on its own worker thread so it never
delays detections, and the detection worker processes frames that arrive
mid-pass instead of skipping one camera frame per pass.
The dashboard sliders/presets (Kalman noise, lookahead, dead zone, replan
threshold, move timing, speed) tune this controller live; `tracking_gain` only
affects idle motion. Controller-specific settings live in
`config/body_profiles.yaml` under `new.head_tracking.gaze`.

### Visual-servo tracking (optional)

Face centering uses timestamped, actual-size detection frames. A visual controller
locks onto one spatially/identity-consistent face over several fresh frames.
It makes at most one bounded correction per new post-settling observation,
using a damped inverse camera/servo model. Old images are never repeatedly
treated as new feedback. Ambiguous saturated-confidence multi-face results,
implausible jumps, and stale results hold position rather than select another
face. This does not repair an unreliable face detector.

Pan and optional tilt execute quintic motion profiles with configured
speed/acceleration bounds. Tilt commands stay
within both the configured tilt travel limits and 20 degrees either side of
their midpoint; tilt holds position when the face is lost or tracking is off.
Set `servo.tilt_enabled: true` in the selected body profile only on hardware with a connected tilt servo
(the default when omitted is false). PWM hardware cannot detect whether a
servo is physically plugged in. `head_tracking.invert_pan` allows for a reversed
pan mount in the uncalibrated model and `invert_tilt` reverses the tilt servo
output (see above). Active centering settings
live under `head_tracking.visual_servo`; legacy tuning sliders/presets affect
idle/person-seek motion, not the face controller. Decorative saccades and speech
offsets do not perturb active centering.

The default visual response uses a 0.5 correction gain, steps up to 5 degrees,
30 degrees/second peak speed, 90 degrees/second-squared peak acceleration, and
0.1 seconds of post-motion settling. Larger corrections and shorter settling
reduce stop-start pauses while the gentler acceleration softens each transition.
Corrections still wait for an image
captured after motion and settling; camera/inference latency therefore adds to
response time. A saved empirical model can require a longer settling interval.

When person seek is enabled and no face is being followed, a detected person's
bounding-box centre drives pan and, where tilt hardware is available, tilt, one
correction per detection after the preceding move and settling have completed.
On the new body these smoothstep moves share the visual controller's step,
speed, and acceleration limits. A tracked face always takes priority. Person seek is
independent of the face-tracking switch: with face tracking off the head follows
the person even while a face is visible. All automatic movement requires enabled,
ready servos. Person seek additionally requires enabled object detection and its
own toggle; disabling detection clears its target. Face tracking requires its
own toggle, but does not require object detection.

The largest detected person is preferred, regardless of detection confidence.
An existing target is retained until a challenger is more than 15% larger,
reducing rapid switching between similarly sized people. Only fresh, confirmed
detections are used for tracking; held overlay boxes do not refresh targets.
The defaults are configurable through
`head_tracking.visual_servo.person_switch_margin` and `face_handoff_s`.
If no eligible target exists, random idle movement uses its independent toggle;
otherwise the head holds position.

Displaying a face label hides only its associated person box, regardless of the
face-tracking toggle. A person is associated when its box contains the face
centre; the smallest containing box wins when boxes overlap. Other people stay
labeled. Face/object image dimensions are accounted for, and stale or differently
rotated face observations do not suppress person labels. The association freshness
window is `object_detection.face_label_max_age_s` (one second by default).

Dashboard pan and tilt sliders send commands immediately as they move; no Go
button is needed. Each axis keeps only the latest pending input while a command
is in flight. Both sliders follow VERA's reported positions, including tracking
movement, except while being adjusted or sending a command. Command errors
appear beside the relevant slider. Tilt controls remain hidden on pan-only bodies.

### Automatic tracking calibration

On the new body, use **Calibrate visual tracking** in the tracking-tuning panel.
Enable tracking, keep exactly one face still and visible, and manually position
the head so both available axes have at least the configured probe-step margin.
The procedure takes several minutes, announces and waits for speech completion
before each move, and tests repeated small positive/negative steps per axis.
It fits normalized image displacement per commanded servo degree, including
cross-axis coupling on tilt-capable hardware. An expiring motion lease prevents
other movement publishers from interfering; disable, stop, and relax remain
available. Cancel stops motion and discards the candidate model.

Calibration rejects excessive noise, inconsistent repeated/held-out responses,
unobservable axes, changed geometry/limits, stale or lost targets, and timeouts.
It then checks live centering within 2% of each controllable image dimension for
multiple observations before saving. Unreachable targets fail validation rather
than widen limits. A failed attempt leaves the previous file untouched.
Successful calibration is stored atomically in ignored
`config/tracking_calibration.json`; camera size/rotation, PWM calibration,
channels, range, tilt capability, or limit changes invalidate it. Back up or
remove that file to revert to the conservative geometry-based model.

`motion.position` is commanded position, not shaft feedback. Camera timestamps
record frame availability from the capture thread, not hardware exposure time.
The fitted response budget includes command execution, observed settling, and
detection cadence; it is deliberately conservative, not a precision motor-delay
measurement. Independent target motion cannot always be distinguished from
camera motion, so calibration requires a stationary person. No online learning
or automatic unannounced calibration movements run at startup.

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Face detection | ✅ (auto) | ✅ (camera feed overlay) | ✅ describe-scene |
| Face tracking (servo follows face) | ✅ `vera face-tracking` | ✅ Settings panel | ✅ face-tracking |
| Person seek (body tracking fallback) | ✅ `vera person-seek` | ✅ Settings panel | ✅ person-seek |
| Random idle motion | ✅ `vera random-motion` | ✅ Settings panel | ✅ random-motion |
| Object detection (Hailo COCO) | ✅ `vera object-detection` | ✅ Settings panel | ✅ object-detection |
| Scene description (LLM) | ✅ `vera vision describe` | ❌ | ✅ describe-scene |
| Privacy / nudity detection | ✅ `vera privacy` | ✅ Settings panel | ✅ privacy |
| Pan/tilt servos and channel settings | ✅ `vera servo pan/tilt/channels` | ✅ Servo controls | ✅ pan-camera |
| Camera snapshot | ✅ `vera snapshot` | ✅ Camera feed | ✅ grab-frame |
| Camera resolution (cam1) | ✅ `vera camera resolution` | ❌ | ❌ |
| Camera rotation | ✅ `vera camera rotation` | ❌ | ❌ |

---

## Depth Estimation

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Dense stereo depth (SGBM) | ✅ `vera depth dense-enable/disable` | ✅ Depth panel | ✅ depth-toggle |
| Monocular neural depth (Hailo) | ✅ `vera depth mono-enable/disable` | ✅ Depth panel | ✅ depth-toggle |
| Depth query (distances) | ✅ `vera depth query` | ✅ Depth panel | ✅ depth-query |
| Stereo calibration | ✅ `scripts/calibrate_stereo.py` | ❌ | ❌ |

---

## Face Registry

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Name a face | ✅ `vera face meet` | ✅ Face panel | ✅ faces |
| List faces | ✅ `vera face list` | ✅ Face panel | ✅ faces |
| Refresh embeddings | ✅ `vera face refresh` | ✅ Face panel | ✅ faces |
| Forget all faces | ✅ `vera face forget-all` | ✅ Face panel | ✅ faces |
| Merge face identities | ✅ `vera face merge` | ❌ | ❌ |
| Greeting cooldown | ✅ `vera face greeting-cooldown` | ✅ Face settings | ❌ |

---

## Audio

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Speak text (TTS) | ✅ `vera say` | ❌ | ✅ say |
| Record audio | ✅ `vera record` | ❌ | ✅ record |
| Play back recording | ✅ `vera playback` | ❌ | ✅ playback |
| EQ preset | ✅ `vera eq set` | ✅ Audio panel | ❌ |
| Audio backend config | ✅ `vera audio` | ✅ Audio panel | ✅ audio |
| Voice STT setup/enable | ✅ `vera audio stt-*` | ✅ Audio panel | ✅ audio |
| Volume | ✅ `vera music volume` | ✅ Music panel | ✅ music |

---

## Music (Pandora via pianobar)

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Play / stop | ✅ `vera music play/stop` | ✅ Music panel | ✅ music |
| Skip song | ✅ `vera music next` | ✅ Music panel | ✅ music |
| Pause / resume | ✅ `vera music pause` | ✅ Music panel | ✅ music |
| Station select | ✅ `vera music play <station>` | ✅ Music panel | ✅ music |
| Thumbs up/down | ✅ `vera music thumbs-up/down` | ✅ Music panel | ✅ music |
| List stations | ✅ `vera music stations` | ✅ Music panel | ✅ music |
| Now playing status | ✅ `vera music status` | ✅ Music panel | ✅ music |

---

## IoT / Smart Home

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Radon monitor | ✅ `vera radon` | ❌ | ✅ radon |
| DROP water softener | ✅ `vera drop` | ❌ | ✅ drop |
| IoT plugin CRUD | ✅ `vera iot` | ✅ IoT panel | ❌ |
| Yale lock | ✅ `vera lock` | ✅ Lock panel (via IoT) | ❌ |
| Nest thermostat | ❌ | ✅ Nest card | ❌ |

---

## System & Thermal

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| System status / health | ✅ `vera status` | ✅ Status bar | ✅ system-status |
| Fan override | ✅ `vera fan set/auto` | ✅ Fan panel | ❌ |
| Fan curve (control points) | ✅ `vera fan curve-set` | ✅ Fan panel | ✅ fan-control |
| Fan tachometer | ✅ `vera fan tach-status` | ✅ Fan panel | ❌ |
| Temperature blend | ✅ (via config) | ✅ Fan panel | ❌ |
| Announce time | ✅ `vera time` | ❌ | ✅ time |
| Software version | ✅ `vera system version` | ✅ About footer | ✅ version |
| Reboot / shutdown | ✅ `vera system reboot/shutdown` | ✅ Settings | ✅ power |
| Restart daemon | ✅ `vera system restart` | ✅ Settings | ❌ |

---

## Quiet Hours

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Enable / disable | ✅ `vera quiet-hours enable/disable` | ✅ Settings panel | ✅ quiet-hours |
| Configure window | ✅ `vera quiet-hours set` | ✅ Settings panel | ✅ quiet-hours |
| Status | ✅ `vera quiet-hours status` | ✅ Settings panel | ✅ quiet-hours |

---

## Skills (OpenClaw voice)

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| List skills | ✅ `vera skills list` | ✅ Skills panel | ❌ |
| Enable / disable skill | ✅ `vera skills enable/disable` | ✅ Skills panel | ❌ |
| Configure skill | ✅ `vera skills config` | ✅ Skills panel | ❌ |

---

## Room Awareness

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Get current room | ✅ `vera room get` | ✅ Status bar | ❌ |
| Set room | ✅ `vera room set` | ✅ Settings | ❌ |
| Auto room detection | ✅ (auto, LLM) | ✅ (auto) | ❌ |

---

## Miscellaneous

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Tell a joke | ✅ `vera joke` | ❌ | ✅ joke |
| Email monitoring | ❌ | ❌ | ✅ email-monitor |
| Bus topic monitor | ✅ `vera watch` | ❌ | ❌ |
| Publish raw bus event | ✅ `vera publish` | ❌ | ❌ |
| Face greeting (auto) | ✅ (auto) | ✅ (auto) | ❌ |
| Guest intro cooldown | ✅ `vera face greeting-cooldown` | ✅ Face settings | ❌ |

---

*Last updated: 2025-06-01. See [CHANGELOG.md](../CHANGELOG.md) for version history.*
