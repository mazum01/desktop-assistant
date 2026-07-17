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

Available skills (25):

| Skill | Description |
|-------|-------------|
| **pan-camera** | "look left", "pan to 90°", "face me" |
| **grab-frame** | "take a photo", "snapshot from camera 2" |
| **describe-scene** | "what do you see?", "describe your surroundings" |
| **face-tracking** | Enable/disable servo face-following behavior |
| **random-motion** | Enable/disable idle random head movement |
| **person-seek** | Enable/disable body-tracking when no face is locked |
| **object-detection** | Toggle Hailo-8 COCO object classification |
| **depth-query** | "how far away is that?", distance scan of current view |
| **depth-toggle** | Enable/disable dense stereo or monocular depth estimation |
| **system-status** | CPU, temp, fan, servo, camera FPS, face count |
| **say** | Speak any text aloud via TTS |
| **time** | Announce the current time |
| **version** | Speak the current software version number |
| **joke** | Tell a random dad joke |
| **quiet-hours** | Configure TTS silence window |
| **music** | Pandora playback (play, skip, thumbs-up, station select…) |
| **record** | Record microphone audio to WAV |
| **playback** | Play back a recorded WAV clip |
| **radon** | Read EcoQube basement radon monitor |
| **drop** | DROP water softener system status |
| **fan-control** | Query or set fan thermal control curve |
| **power** | Reboot or shut down the Raspberry Pi |
| **email-monitor** | Check Gmail for important unread messages |
| **faces** | Face registry management |
| **privacy** | Nudity detection look-away (enable/disable/status) |

See [`docs/FEATURES.md`](docs/FEATURES.md) for the full CLI / Web / OpenClaw feature matrix.

## Status
Active development — see `/VERSION` for the current release and [CHANGELOG.md](CHANGELOG.md) for history.

## License
Apache 2.0 — see [LICENSE](LICENSE)
