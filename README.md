# VERA — Vision-Enabled Reasoning Agent

A semi-animated desktop AI assistant built on Raspberry Pi 5 with Hailo-8 AI
acceleration, stereo vision, dual microphones, a panning servo head, and
temperature-controlled cooling.

## Hardware
- Raspberry Pi 5
- Hailo-8 AI accelerator
- Stereo cameras
- Dual microphones
- DS3218 servo (pan, 270° range) on SparkFun Pi Servo pHAT
- SparkFun Qwiic TMP117 temperature sensor
- Noctua NF-A6x25 PWM fan
- Sabrent USB audio adapter → stereo speakers

## Documents
- [Project Phases](docs/PROJECT_PHASES.md)
- [Requirements](docs/REQUIREMENTS.md)

## Repo Layout
```
config/          runtime configuration
docs/            printable design docs
hardware/        per-device drivers and notes
.github/skills/  OpenClaw AI gateway skills (Telegram/Claude integration)
scripts/         bring-up and utility scripts
services/        long-running daemons / systemd units
src/             application code
  core/            shared utilities
  perception/      vision + audio understanding
  motion/          pan servo controller
  thermal/         temperature + fan control
  audio/           capture & playback
  vision/          camera pipeline
  assistant/       dialog & skills
tests/           unit and integration tests
```

## OpenClaw Integration
Natural-language control via Telegram using the [OpenClaw](https://openclaw.dev)
AI gateway. See [`.github/skills/README.md`](.github/skills/README.md) for installation.

Available skills:
- **pan-camera** — "look left", "pan to 90°", "face me"
- **grab-frame** — "take a photo", "snapshot from camera 2"

## Status
Phase 0 — project scaffold.

## License
Apache 2.0 — see [LICENSE](LICENSE)
