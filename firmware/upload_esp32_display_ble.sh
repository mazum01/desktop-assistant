#!/usr/bin/env bash
# Uploads a compiled firmware binary to the VERA ESP32-C6 LCD over Bluetooth
# Low Energy, using the NimBLEOta GATT service compiled into
# firmware/vera_display/vera_display.ino (see README.md's "BLE OTA firmware
# updates" section). Requires the device to already be running a firmware
# build that includes NimBLEOta — the very first such build must still be
# flashed once over USB via upload_esp32_display.sh.
#
# Environment:
#   VERA_VERBOSE=1               echo every command, pass -v to arduino-cli
#   VERA_ESP32_BLE_ADDRESS=...   target BLE MAC (default AC:EB:E6:23:EC:76)
#   VERA_PYTHON=...              python interpreter to run the uploader with
#   VERA_SKIP_BUILD=1            upload the existing .bin, skip recompiling
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIRMWARE_DIR="$ROOT/firmware"
SKETCH_DIR="$FIRMWARE_DIR/vera_display"
UPLOADER="$FIRMWARE_DIR/nimbleota_uploader.py"
BUILD_OUT="$FIRMWARE_DIR/.arduino-build-ble-ota"

ADDRESS="${VERA_ESP32_BLE_ADDRESS:-AC:EB:E6:23:EC:76}"
PYTHON="${VERA_PYTHON:-python3}"
CORE_UNIT="desktop-assistant-core.service"

source "$FIRMWARE_DIR/lib_log.sh"

info "target:  $ADDRESS"
info "sketch:  $SKETCH_DIR"
info "python:  $(command -v "$PYTHON" || echo "$PYTHON")"
info "verbose: $VERA_VERBOSE"
printf '\n'

[[ -f "$UPLOADER" ]] || fatal "missing OTA uploader script: $UPLOADER"

# ── Build ──────────────────────────────────────────────────────────────
# build_esp32_display.sh emits straight into BUILD_OUT via BUILD_OUTPUT_DIR,
# so we get a stable path to the .bin without a second full compile.
if [[ "${VERA_SKIP_BUILD:-0}" == "1" ]]; then
    step "Skipping build (VERA_SKIP_BUILD=1)"
    step_done
else
    BUILD_QUIET_HEADER=1 BUILD_OUTPUT_DIR="$BUILD_OUT" \
        "$FIRMWARE_DIR/build_esp32_display.sh"
fi

step "Locating firmware binary"
BIN_FILE="$(find "$BUILD_OUT" -maxdepth 1 -name '*.ino.bin' | head -n1)"
[[ -n "$BIN_FILE" ]] || fatal "could not find compiled .bin under $BUILD_OUT"
info "binary: $BIN_FILE"
info "size:   $(stat -c%s "$BIN_FILE") bytes"
step_done

# ── BLE link handover ──────────────────────────────────────────────────
# DisplayService (in desktop-assistant-core) holds a persistent BLE link to
# the display. While it is connected the ESP32 stops advertising, so the
# uploader's scan-based connect fails with "Device with address ... was not
# found". Park the daemon with SIGSTOP (a plain `systemctl stop` isn't in the
# passwordless sudo allowlist, and killing it would just trip
# Restart=on-failure) and drop the link so the device advertises again for
# the duration of the OTA.
CORE_PID=""

release_ble_link() {
    CORE_PID="$(systemctl show -p MainPID --value "$CORE_UNIT" 2>/dev/null || true)"
    if [[ -z "$CORE_PID" || "$CORE_PID" == "0" ]]; then
        CORE_PID=""
        info "$CORE_UNIT not running — nothing to pause"
        return
    fi
    info "pausing $CORE_UNIT (pid $CORE_PID) with SIGSTOP"
    vrun sudo -n kill -STOP "$CORE_PID"
    info "dropping existing BLE link to $ADDRESS"
    vrun bluetoothctl disconnect "$ADDRESS" >/dev/null 2>&1 || true
    sleep 2
    local state
    state="$(bluetoothctl info "$ADDRESS" 2>/dev/null | awk -F': ' '/Connected/{print $2}')"
    info "device connected state is now: ${state:-unknown}"
}

restore_ble_link() {
    if [[ -n "$CORE_PID" ]]; then
        printf '\n'
        info "resuming $CORE_UNIT (pid $CORE_PID) with SIGCONT"
        sudo -n kill -CONT "$CORE_PID" || warn "failed to resume pid $CORE_PID — run: sudo kill -CONT $CORE_PID"
        CORE_PID=""
    fi
}

# Always hand the display back to the daemon, even on failure or Ctrl-C.
trap restore_ble_link EXIT INT TERM

step "Releasing BLE link from $CORE_UNIT"
release_ble_link
step_done

# ── Upload ─────────────────────────────────────────────────────────────
step "Uploading firmware over BLE OTA"
info "this takes several minutes; progress is reported per 4KB sector"
vrun "$PYTHON" -u "$UPLOADER" "$BIN_FILE" "$ADDRESS"
step_done "device will reboot into the new firmware"
