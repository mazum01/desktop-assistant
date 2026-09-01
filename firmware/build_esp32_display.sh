#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$ROOT/firmware"
SKETCH_DIR="$FIRMWARE_DIR/vera_display"
CLI_CONFIG="$FIRMWARE_DIR/arduino-cli.yaml"
FQBN="${VERA_ESP32_FQBN:-esp32:esp32:esp32c6}"
CLI="${ARDUINO_CLI:-arduino-cli}"

if ! command -v "$CLI" >/dev/null 2>&1; then
    printf 'arduino-cli is required. Install the latest release from https://arduino.github.io/arduino-cli/latest/installation/\\n' >&2
    exit 127
fi

export ARDUINO_DATA_DIR="$FIRMWARE_DIR/.arduino-data"
export ARDUINO_DOWNLOADS_DIR="$FIRMWARE_DIR/.arduino-downloads"

"$CLI" --config-file "$CLI_CONFIG" config dump >/dev/null
"$CLI" --config-file "$CLI_CONFIG" core update-index
"$CLI" --config-file "$CLI_CONFIG" core install esp32:esp32
"$CLI" --config-file "$CLI_CONFIG" lib install "GFX Library for Arduino"
"$CLI" --config-file "$CLI_CONFIG" lib install "NimBLE-Arduino"
"$CLI" --config-file "$CLI_CONFIG" lib install "ArduinoJson"

# NimBLEOta (BLE OTA firmware update support) is not published in the
# Arduino Library Manager index, so it can't be fetched with a plain
# `lib install <name>`. Install it directly from its upstream git repo
# instead (requires explicitly opting into --git-url, which arduino-cli
# disables by default since it can install untrusted code).
if [[ ! -d "$(pwd)/libraries/NimBLEOta" ]]; then
    ARDUINO_LIBRARY_ENABLE_UNSAFE_INSTALL=true "$CLI" --config-file "$CLI_CONFIG" \
        lib install --git-url https://github.com/h2zero/NimBLEOta.git
fi

"$CLI" --config-file "$CLI_CONFIG" compile --fqbn "$FQBN" "$SKETCH_DIR"
