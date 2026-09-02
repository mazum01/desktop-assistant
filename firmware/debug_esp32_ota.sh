#!/usr/bin/env bash
# Diagnostic helper for the BLE OTA sector-0 stall.
#
# Builds vera_display with NimBLE's own logging enabled (Core Debug Level =
# verbose) and USB CDC on boot, flashes it over USB, then tails the serial
# console so NimBLEOta's NIMBLE_LOGE/NIMBLE_LOGW messages are visible while a
# BLE OTA upload is attempted from another terminal.
#
# The stock build uses DebugLevel=none, which compiles every NIMBLE_LOG* call
# out entirely -- that is why firmwareOnWrite()'s silent returns have been
# invisible.
#
# Usage:
#   1. Connect the display over USB.
#   2. bash firmware/debug_esp32_ota.sh
#   3. In another terminal: bash firmware/upload_esp32_display_ble.sh
#   4. Watch this terminal for the reason the device rejects sector 0.
#
# Env:
#   VERA_ESP32_PORT=/dev/ttyACM0   serial port override
#   VERA_SKIP_FLASH=1              just tail the console, don't rebuild/flash
set -euo pipefail

FIRMWARE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib_log.sh
source "$FIRMWARE_DIR/lib_log.sh"

SKETCH_DIR="$FIRMWARE_DIR/vera_display"
BUILD_DIR="$FIRMWARE_DIR/.arduino-build-ota-debug"
CLI="${ARDUINO_CLI:-arduino-cli}"
# Verbose NimBLE logging + USB CDC so the logs actually reach the host.
FQBN="esp32:esp32:esp32c6:CDCOnBoot=cdc,DebugLevel=verbose"

export ARDUINO_DIRECTORIES_DATA="$FIRMWARE_DIR/.arduino-data"
export ARDUINO_DIRECTORIES_DOWNLOADS="$FIRMWARE_DIR/.arduino-downloads"
export ARDUINO_DIRECTORIES_USER="$FIRMWARE_DIR"

detect_port() {
    if [[ -n "${VERA_ESP32_PORT:-}" ]]; then
        echo "$VERA_ESP32_PORT"
        return
    fi
    for candidate in /dev/ttyACM* /dev/ttyUSB*; do
        [[ -e "$candidate" ]] && { echo "$candidate"; return; }
    done
    return 1
}

PORT="$(detect_port)" || fatal "no serial port found — connect the display over USB first"

info "port:  $PORT"
info "fqbn:  $FQBN"
info "build: $BUILD_DIR"

if [[ "${VERA_SKIP_FLASH:-0}" != "1" ]]; then
    step "Compiling with verbose NimBLE logging"
    info "this rebuilds from scratch because the debug flags differ from the normal build"
    vrun "$CLI" compile $(cli_verbose_flags) \
        --fqbn "$FQBN" --output-dir "$BUILD_DIR" "$SKETCH_DIR"
    step_done

    step "Flashing over USB"
    vrun "$CLI" upload $(cli_verbose_flags) \
        --fqbn "$FQBN" --port "$PORT" --input-dir "$BUILD_DIR" "$SKETCH_DIR"
    step_done
fi

step "Tailing serial console"
info "now run in another terminal:  bash firmware/upload_esp32_display_ble.sh"
info "look for lines such as:"
info "  'Sector index error, expected: N, received: M'"
info "  'packet sequence error'"
info "  'Received write from unknown client - ignored'"
info "  'ota not started'"
info "press Ctrl-C to stop"
echo

# The device re-enumerates after flashing; wait for the port to come back.
for _ in $(seq 1 20); do
    [[ -e "$PORT" ]] && break
    sleep 0.5
done

exec "${VERA_PYTHON:-python3}" -u -c "
import sys, time
try:
    import serial
except ImportError:
    sys.exit('pyserial not installed: pip install --user pyserial')
port = '$PORT'
while True:
    try:
        with serial.Serial(port, 115200, timeout=1) as ser:
            print(f'--- connected to {port} ---', flush=True)
            while True:
                line = ser.readline()
                if line:
                    sys.stdout.write(line.decode('utf-8', 'replace'))
                    sys.stdout.flush()
    except Exception as err:
        print(f'--- {type(err).__name__}: {err}; retrying ---', flush=True)
        time.sleep(2)
"
