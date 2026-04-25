# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
