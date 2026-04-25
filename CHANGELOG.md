# Changelog

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versioning follows [Semantic Versioning](https://semver.org/).

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
