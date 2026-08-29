#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$ROOT/firmware"
SKETCH_DIR="$FIRMWARE_DIR/vera_display"
CLI_CONFIG="$FIRMWARE_DIR/arduino-cli.yaml"
FQBN="${VERA_ESP32_FQBN:-esp32:esp32:esp32c6}"
PORT="${VERA_ESP32_PORT:-}"
CLI="${ARDUINO_CLI:-arduino-cli}"

if ! command -v "$CLI" >/dev/null 2>&1; then
    printf 'arduino-cli is required. Install the latest release from https://arduino.github.io/arduino-cli/latest/installation/\\n' >&2
    exit 127
fi
if [[ -z "$PORT" ]]; then
    printf 'Set VERA_ESP32_PORT to the ESP32 serial port (for example /dev/ttyACM0).\\n' >&2
    printf 'Detected ports:\\n' >&2
    "$CLI" board list || true
    exit 2
fi

export ARDUINO_DATA_DIR="$FIRMWARE_DIR/.arduino-data"
export ARDUINO_DOWNLOADS_DIR="$FIRMWARE_DIR/.arduino-downloads"

"$ROOT/firmware/build_esp32_display.sh"
"$CLI" --config-file "$CLI_CONFIG" upload --fqbn "$FQBN" --port "$PORT" "$SKETCH_DIR"
