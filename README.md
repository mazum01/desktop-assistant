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
- DS3218 servo (pan, 270° range) on SparkFun Pi Servo pHAT
- SparkFun Qwiic TMP117 temperature sensor
- Noctua NF-A6x25 PWM fan
- Sabrent USB audio adapter → stereo speakers

## Documents
- [Project Phases](docs/PROJECT_PHASES.md)
- [Requirements](docs/REQUIREMENTS.md)
- [Feature Parity Matrix](docs/FEATURES.md)
- [Architecture Review](docs/ARCHITECTURE_REVIEW.md) ([printable PDF](docs/ARCHITECTURE_REVIEW.pdf))
- [Process Isolation Proposal](docs/architecture/PROCESS_ISOLATION_PROPOSAL.md)
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
  motion/          pan servo controller
  thermal/         temperature + fan control
  audio/           capture & playback, ReSpeaker XVF control
  voice/           voice command pipeline (wake word, STT, dialog)
  vision/          camera pipeline
  iot/             smart-home integrations (Nest, Yale, etc.)
  watchdog/        health monitor / restart supervisor
  web/             web GUI (static assets) served by the web service
tests/           unit and integration tests
```

VERA is split across several independent OS processes (ZeroMQ IPC), not one
monolith: `desktop-assistant-core`, `desktop-assistant-thermal`,
`desktop-assistant-media`, `desktop-assistant-integrations`, and
`desktop-assistant-watchdog` are separate systemd units today, with more
services (IoT, Skills) planned to move out of `core` per the
[Process Isolation Proposal](docs/architecture/PROCESS_ISOLATION_PROPOSAL.md).

## OpenClaw Integration
Natural-language control via Telegram using the [OpenClaw](https://openclaw.dev)
AI gateway. See [`.github/skills/README.md`](.github/skills/README.md) for installation.

Available skills (26, see [`.github/skills/`](.github/skills/)):

| Skill | Description |
|-------|-------------|
| **pan-camera** | "look left", "pan to 90°", "face me" |
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
