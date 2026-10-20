# VERA — Vision-Enabled Reasoning Agent

A semi-animated desktop AI assistant built on Raspberry Pi 5 with Hailo-8 AI
acceleration, stereo vision, a ReSpeaker microphone array, a panning servo
head, and temperature-controlled cooling.

## Hardware
- Raspberry Pi 5
- Hailo-8 AI accelerator
- Stereo cameras
- Seeed Studio ReSpeaker Flex XVF3800 (6-mic USB array with onboard AEC,
  beamforming, and noise suppression; USB vendor-control channel for
  DSP tuning/telemetry)
- Pan and head-tilt servos (180° range) on SparkFun Pi Servo pHAT
  (legacy geared DS3218 pan configurations remain supported)
- TMP117 (VERA3) or TMP102 (VERA) temperature sensor, auto-detected over I²C
- Noctua NF-A6x25 PWM fan
- Sabrent USB audio adapter → stereo speakers

## Documents
- [Project Phases](docs/PROJECT_PHASES.md)
- [Requirements](docs/REQUIREMENTS.md)
- [Feature Parity Matrix](docs/FEATURES.md)
- [Architecture Review](docs/ARCHITECTURE_REVIEW.md) ([printable PDF](docs/ARCHITECTURE_REVIEW.pdf))
- [Process Isolation Proposal](docs/architecture/PROCESS_ISOLATION_PROPOSAL.md)
- [Clean Raspberry Pi OS Installation (VERA3)](docs/VERA3_SETUP.md)
- [Automatic releases and device updates](docs/UPDATES.md)
- [TODO](docs/TODO.md)

## Repo Layout
```
config/          runtime configuration
firmware/        Arduino firmware for attached ESP32 display hardware
docs/            printable design docs (incl. architecture/ proposals)
hardware/        per-device drivers and notes (audio, perception, servo, thermal, vision)
.github/skills/  OpenClaw AI gateway skills (Telegram/Claude integration)
scripts/         bring-up and utility scripts
services/        systemd units, udev rules
src/             application code
  assistant/       top-level orchestration / entry point
  core/            shared utilities, IPC bridge, process-isolation scaffolding
  services/        service layer (audio, vision, media, IoT, skills dispatch, etc.)
  skills/          in-process skill implementations (describe-scene, depth-query, ...)
  perception/      vision + audio understanding
  motion/          pan/tilt servo controller
  thermal/         temperature + fan control
  audio/           capture & playback, ReSpeaker XVF control
  voice/           voice command pipeline (wake word, STT, dialog)
  vision/          camera pipeline
  iot/             smart-home integrations (Nest, Yale, etc.)
  watchdog/        health monitor / restart supervisor
  updater/         staged stable-release deployment and recovery controller
  web/             web GUI (static assets) served by the web service
tests/           unit and integration tests
```

VERA is split across several independent OS processes (ZeroMQ IPC), not one
monolith: `desktop-assistant-core`, `desktop-assistant-thermal`,
`desktop-assistant-media`, `desktop-assistant-integrations`, and
`desktop-assistant-watchdog` are separate systemd units today, with more
services (IoT, Skills) planned to move out of `core` per the
[Process Isolation Proposal](docs/architecture/PROCESS_ISOLATION_PROPOSAL.md).

## Pan and head tilt

Both installed servos are direct-drive 180° models: center 90°, initial safe
travel 70–110°. Pan defaults to PCA9685 channel 0, tilt to channel 1.
Set different, distinct channels (0–15) in the web Controls tab or CLI:

```bash
vera servo channels --pan 0 --tilt 1
vera servo status
vera servo pan --to 90
vera servo tilt --to 90 --move-time-ms 1000
vera servo limits --axis pan --min 70 --max 110
vera servo limits --axis tilt --min 70 --max 110
```

Channels and travel limits persist across restarts. Reassigning channels
releases both old outputs and clears queued moves without moving new outputs.
Disable autonomous tracking before rewiring or changing assignments.
The REST API exposes `GET/PUT /api/settings/servo/channels`,
`PUT /api/settings/servo/tilt-limits`, and `POST /api/tilt` (`{"angle": 90}`).
`motion.position` retains pan `angle` and adds `tilt_angle`.
Base calibration belongs in `config/assistant.yaml` under `servo`:
`range_deg`, `pulse_min_us`, `pulse_max_us`, channels, and axis limits.
`range_deg: 180` uses direct angular mapping; omitted/360 retains the legacy
geared pan mapping. Tilt always uses 180° direct mapping.

## OpenClaw Integration
Natural-language control via Telegram using the [OpenClaw](https://openclaw.dev)
AI gateway. See [`.github/skills/README.md`](.github/skills/README.md) for installation.

Available skills (26, see [`.github/skills/`](.github/skills/)):

| Skill | Description |
|-------|-------------|
| **pan-camera** | "look left", "tilt to 90°", "set pan channel 0 and tilt channel 1" |
| **grab-frame** | "take a photo", "snapshot from camera 2" |
| **describe-scene** | "what do you see?", "describe your surroundings" |
| **face-tracking** | Enable/disable servo face-following behavior |
| **random-motion** | Enable/disable idle random head movement |
| **object-detection** | Toggle Hailo-8 COCO object classification |
| **depth-query** | "how far away is that?", distance scan of current view |
| **depth-toggle** | Enable/disable dense stereo or monocular depth estimation |
| **system-status** | CPU, temp, fan, servo, camera FPS, face count, running services |
| **say** | Speak any text aloud via TTS |
| **time** | Announce the current time |
| **version** | Speak the current software version number |
| **joke** | Tell a random dad joke |
| **quiet-hours** | Configure TTS silence window |
| **music** | Pandora playback (play, skip, thumbs-up, station select…) |
| **podcast** | Apple Podcasts control (search, subscribe, play, pause, status) |
| **audio** | Volume, EQ preset, mic gain, mute, audio backend selection |
| **record** | Record microphone audio to WAV |
| **playback** | Play back a recorded WAV clip |
| **radon** | Read EcoQube basement radon monitor |
| **drop** | DROP water softener system status |
| **fan-control** | Query or set fan thermal control curve |
| **power** | Reboot or shut down the Raspberry Pi |
| **privacy** | Nudity detection look-away (enable/disable/status) |
| **restart-daemon** | Restart `desktop-assistant-core` after code changes (Copilot CLI) |
| **security-scan** | Audit the codebase for secrets/vulnerabilities, log findings to `docs/TODO.md` |

See [`docs/FEATURES.md`](docs/FEATURES.md) for the full CLI / Web / OpenClaw feature matrix.

## Status
Active development — see `/VERSION` for the current release and [CHANGELOG.md](CHANGELOG.md) for history.

## License
Apache 2.0 — see [LICENSE](LICENSE)
