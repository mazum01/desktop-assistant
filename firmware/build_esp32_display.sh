#!/usr/bin/env bash
# Builds the VERA ESP32-C6 LCD display firmware.
#
# Environment:
#   VERA_VERBOSE=1          echo every command, pass -v to arduino-cli
#   VERA_ESP32_FQBN=...     override the board FQBN
#   ARDUINO_CLI=...         override the arduino-cli binary
#   BUILD_OUTPUT_DIR=...    also emit the build artefacts (.bin etc) here
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$ROOT/firmware"
SKETCH_DIR="$FIRMWARE_DIR/vera_display"
CLI_CONFIG="$FIRMWARE_DIR/arduino-cli.yaml"
FQBN="${VERA_ESP32_FQBN:-esp32:esp32:esp32c6}"
CLI="${ARDUINO_CLI:-arduino-cli}"

source "$FIRMWARE_DIR/lib_log.sh"

# Callers that build as one phase of a larger job (upload_esp32_display_ble.sh)
# set this to suppress the standalone banner.
BUILD_QUIET_HEADER="${BUILD_QUIET_HEADER:-0}"

if ! command -v "$CLI" >/dev/null 2>&1; then
    fatal "arduino-cli is required. Install the latest release from https://arduino.github.io/arduino-cli/latest/installation/"
fi

if [[ "$BUILD_QUIET_HEADER" != "1" ]]; then
    info "sketch:  $SKETCH_DIR"
    info "fqbn:    $FQBN"
    info "cli:     $(command -v "$CLI")"
    info "verbose: $VERA_VERBOSE"
    printf '\n'
fi

export ARDUINO_DATA_DIR="$FIRMWARE_DIR/.arduino-data"
export ARDUINO_DOWNLOADS_DIR="$FIRMWARE_DIR/.arduino-downloads"

step "Refreshing package index"
vrun "$CLI" --config-file "$CLI_CONFIG" config dump >/dev/null
vrun "$CLI" --config-file "$CLI_CONFIG" core update-index
step_done

step "Installing esp32 core"
vrun "$CLI" --config-file "$CLI_CONFIG" core install esp32:esp32
step_done

step "Installing Arduino libraries"
for lib in "GFX Library for Arduino" "NimBLE-Arduino" "ArduinoJson"; do
    info "library: $lib"
    vrun "$CLI" --config-file "$CLI_CONFIG" lib install "$lib"
done
step_done

# NimBLEOta (BLE OTA firmware update support) is not published in the
# Arduino Library Manager index, so it can't be fetched with a plain
# `lib install <name>`. Install it directly from its upstream git repo
# instead (requires explicitly opting into --git-url, which arduino-cli
# disables by default since it can install untrusted code).
step "Ensuring NimBLEOta (BLE OTA support)"
if [[ -d "$(pwd)/libraries/NimBLEOta" ]]; then
    info "already present at $(pwd)/libraries/NimBLEOta"
else
    info "not installed — fetching from https://github.com/h2zero/NimBLEOta.git"
    ARDUINO_LIBRARY_ENABLE_UNSAFE_INSTALL=true vrun "$CLI" --config-file "$CLI_CONFIG" \
        lib install --git-url https://github.com/h2zero/NimBLEOta.git
fi
step_done

# When a caller needs the .bin on disk it passes BUILD_OUTPUT_DIR through,
# which saves a second full compile just to relocate the artefacts.
step "Compiling sketch"
if [[ -n "${BUILD_OUTPUT_DIR:-}" ]]; then
    info "output dir: $BUILD_OUTPUT_DIR"
    rm -rf "$BUILD_OUTPUT_DIR"
    vrun "$CLI" --config-file "$CLI_CONFIG" compile $(cli_verbose_flags) \
        --fqbn "$FQBN" --output-dir "$BUILD_OUTPUT_DIR" "$SKETCH_DIR"
else
    vrun "$CLI" --config-file "$CLI_CONFIG" compile $(cli_verbose_flags) \
        --fqbn "$FQBN" "$SKETCH_DIR"
fi
step_done
