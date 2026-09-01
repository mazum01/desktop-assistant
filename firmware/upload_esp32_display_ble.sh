#!/usr/bin/env bash
# Uploads a compiled firmware binary to the VERA ESP32-C6 LCD over Bluetooth
# Low Energy, using the NimBLEOta GATT service compiled into
# firmware/vera_display/vera_display.ino (see README.md's "BLE OTA firmware
# updates" section). Requires the device to already be running a firmware
# build that includes NimBLEOta — the very first such build must still be
# flashed once over USB via upload_esp32_display.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$ROOT/firmware"
SKETCH_DIR="$FIRMWARE_DIR/vera_display"
UPLOADER="$FIRMWARE_DIR/nimbleota_uploader.py"
BUILD_OUT="$FIRMWARE_DIR/.arduino-build-ble-ota"

ADDRESS="${VERA_ESP32_BLE_ADDRESS:-AC:EB:E6:23:EC:76}"
PYTHON="${VERA_PYTHON:-python3}"

if [[ ! -f "$UPLOADER" ]]; then
    printf 'Missing OTA uploader script: %s\n' "$UPLOADER" >&2
    exit 2
fi

"$ROOT/firmware/build_esp32_display.sh"

# The compiled .bin is not normally kept around by build_esp32_display.sh;
# recompile once more with --output-dir so we have a stable path to the
# firmware binary to hand to the OTA uploader.
export ARDUINO_DATA_DIR="$FIRMWARE_DIR/.arduino-data"
export ARDUINO_DOWNLOADS_DIR="$FIRMWARE_DIR/.arduino-downloads"
CLI="${ARDUINO_CLI:-arduino-cli}"
FQBN="${VERA_ESP32_FQBN:-esp32:esp32:esp32c6}"
CLI_CONFIG="$FIRMWARE_DIR/arduino-cli.yaml"

rm -rf "$BUILD_OUT"
"$CLI" --config-file "$CLI_CONFIG" compile --fqbn "$FQBN" --output-dir "$BUILD_OUT" "$SKETCH_DIR"

BIN_FILE="$(find "$BUILD_OUT" -maxdepth 1 -name '*.ino.bin' | head -n1)"
if [[ -z "$BIN_FILE" ]]; then
    printf 'Could not find compiled .bin under %s\n' "$BUILD_OUT" >&2
    exit 3
fi

printf 'Uploading %s to %s over BLE OTA...\n' "$BIN_FILE" "$ADDRESS"
"$PYTHON" "$UPLOADER" "$BIN_FILE" "$ADDRESS"
