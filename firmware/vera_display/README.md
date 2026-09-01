# VERA Waveshare ESP32-C6 LCD firmware

This Arduino sketch is the firmware project for the
[Waveshare ESP32-C6-LCD-1.47](https://www.waveshare.com/product/esp32-related/boards-kits/esp32-c6/esp32-c6-lcd-1.47.htm).
The firmware runs on the ESP32-C6; the Raspberry Pi host communicates with it
over BLE GATT.

## Board contract

- Board: `ESP32-C6`
- Display: 1.47-inch, 172x320 TFT, ST7789 controller, SPI
- BLE: ESP32-C6 Bluetooth 5
- LCD pins: MOSI GPIO6, SCLK GPIO7, CS GPIO14, DC GPIO15, RST GPIO21
- Backlight: GPIO22
- Brightness: keep at or below 50%, per
  [Waveshare's Arduino guidance](https://docs.waveshare.com/ESP32-C6-LCD-1.47/Arduino)

The pin mapping and display orientation must be verified against the board
schematic during hardware validation before production flashing.

## Hardware validation results (physical Waveshare ESP32-C6-LCD-1.47)

Validated end-to-end against the physical board (firmware v1.53.1):

- **BLE discovery/pairing**: device advertises as `VERA-Display`
  (`AC:EB:E6:23:EC:76`); `DisplayService` on the host connects directly by
  address (no interactive pairing required) using `bleak`.
- **Persistent connection**: `DisplayService` holds one long-lived
  `BleakClient` session instead of reconnecting per message — connect takes
  ~1-1.5s once, then writes complete in <10ms each.
- **Reconnect after drop**: forcing a BLE disconnect (e.g. `Device1.Disconnect`
  over D-Bus, host reboot, or ESP32 reset) is detected within one queue-write
  attempt and `DisplayService` automatically reconnects, typically within ~1s.
- **Stale-connection recovery**: if the host process is killed uncleanly,
  BlueZ can be left believing it still holds a connection to the device even
  though nothing is attached, which makes `bleak`'s scan-based connect fail
  permanently with `BleakDeviceNotFoundError`. `DisplayService` detects this
  ("was not found" error) and force-clears the stale state via
  `org.bluez.Device1.Disconnect` over D-Bus before retrying, self-healing
  without manual `bluetoothctl` intervention.
- **Mouth animations**: all seven states (`neutral`, `listening`, `speaking`,
  `happy`, `sad`, `surprised`, `error`) verified visually via
  `da display mouth <state>`.
- **Startup/status text**: `status_renderer.cpp` renders boot progress,
  service-ready, degraded, and error text over the mouth display. Transient
  states (`info`/`ready`, including version announcements) auto-revert to the
  mouth renderer after a 4s hold (`READY_HOLD_MS`); `degraded`/`error` persist
  until resolved.
- **Orientation**: confirmed correct (text and mouth shapes render right-side
  up, matching the board's physical mounting) at the pin mapping above; no
  rotation offset needed in `Arduino_GFX` setup.
- **Refresh performance**: mouth animation renders smoothly at its ~15 fps
  target with no visible tearing; status text redraws are event-driven (no
  polling) so there is no added draw overhead when idle.
- **Failure recovery operational note**: if the display ever gets stuck
  disconnected, first try `bluetoothctl -- disconnect <addr>`; if BlueZ still
  reports `Connected: yes` afterward, the daemon's own auto-recovery will
  clear it within a couple of retry cycles (~2-4s) — no manual `remove`/re-pair
  is required in normal operation.

## Project layout

`vera_display.ino` implements the board scaffold, BLE GATT link (see
[BLE protocol](#ble-protocol) below), and dispatches `"mouth"` commands to the
mouth renderer. Startup/status rendering (text/iconography) is a separate
follow-on task; `"status"` commands with a `degraded`/`error` state are
reflected onto the mouth as a safe fallback until that renderer lands.

`mouth_renderer.h`/`mouth_renderer.cpp` render the primary "mouth imitation"
display using `Arduino_GFX` (`Arduino_ESP32SPI` + `Arduino_ST7789`). Each
emotional state (`neutral`, `listening`, `speaking`, `happy`, `sad`,
`surprised`, `error`) maps to a small geometry/animation parameter set
(width/height fraction, corner radius, oscillation amplitude and period)
rather than a bitmap, so expressions are cheap to draw at ~15 fps and easy to
retune. An unrecognized state defaults to the `error` rendering (distinct red
color) rather than silently freezing on stale content.

`libraries.txt` records the external Arduino library dependencies.

## BLE protocol

- Service UUID, characteristic UUIDs, and device name are defined once in
  `ble_protocol.h` and must match `DisplayServiceConfig` on the host
  (`ble_address`, `ble_characteristic_uuid`, `ble_status_characteristic_uuid`).
- Messages are newline-terminated JSON objects. BLE writes/notifications may
  fragment a message across multiple MTU-sized chunks; both sides
  concatenate bytes until a `\n` is seen before parsing.
- On connect, the firmware notifies a `{"event":"hello",...}` message; on
  disconnect it automatically resumes advertising so the host can reconnect
  without a firmware reboot.

## Arduino CLI workflow

The project includes [`arduino-cli.yaml`](../arduino-cli.yaml) with Espressif's
official board package index. The board core must be ESP32 3.0.0 or newer, as
required by Waveshare. Install the latest Arduino CLI using its
[official installation instructions](https://arduino.github.io/arduino-cli/latest/installation/),
then run:

```bash
./firmware/build_esp32_display.sh
VERA_ESP32_PORT=/dev/ttyACM0 ./firmware/upload_esp32_display.sh
```

The scripts keep Arduino CLI data and downloads under `firmware/` (ignored by
Git), install the ESP32 core and Arduino GFX dependency, and use
`esp32:esp32:esp32c6` by default. Override the board or CLI executable with
`VERA_ESP32_FQBN` or `ARDUINO_CLI` when needed. Upload requires a connected
board and permission to access its serial port.

## BLE OTA firmware updates

Firmware v1.54.0+ includes [`NimBLEOta`](https://github.com/h2zero/NimBLEOta)
(fetched automatically by `build_esp32_display.sh` from its upstream git
repo, MIT licensed), which adds a
second GATT service (UUID `0x8018`) so subsequent firmware builds can be
pushed over the same BLE radio without a USB cable, using the same `bleak`
dependency the host already relies on for `DisplayService`.

- `vera_display.ino` starts the OTA service (`g_ota.start(&g_ota_callbacks)`)
  alongside the existing mouth/status command service, and raises the BLE
  MTU to 517 bytes so 4KB firmware sectors transfer efficiently.
- OTA progress, completion, pause-on-disconnect, and error states are
  surfaced on-screen via the existing `status_renderer` (`"OTA update NN%"`,
  `"OTA complete\nRebooting..."`, etc.), so progress is visible even without
  a host driving normal status commands.
- On completion the ESP32 automatically restarts into the new firmware
  (`ESP.restart()`); if the BLE link drops mid-update, the partial write is
  held open for 30s to allow the uploader to reconnect and resume before
  the update is aborted.

**One-time bootstrap requirement**: this OTA capability does not exist on a
device running older firmware, so the *first* build containing `NimBLEOta`
must still be flashed once over USB serial with the existing workflow above.
Every build after that can be pushed over BLE.

To push a new build over BLE once the device has OTA-capable firmware:

```bash
VERA_ESP32_BLE_ADDRESS=AC:EB:E6:23:EC:76 ./firmware/upload_esp32_display_ble.sh
```

This compiles the sketch, locates the resulting `.bin`, and streams it to the
device using `firmware/nimbleota_uploader.py` (a copy of NimBLEOta's
bundled uploader script)
uploader (chunked 4KB sectors with CRC16 verification and automatic
retry-on-error, per the NimBLEOta wire protocol). `VERA_ESP32_BLE_ADDRESS`
defaults to the address already configured in `config/assistant.yaml`
(`display.ble_address`). Because the ESP32-C6's default partition table
(`Default 4MB with spiffs`) includes dual OTA app slots, no partition scheme
change is required.

**Note**: `DisplayService` on the host holds its own persistent BLE
connection to the display for mouth/status commands. NimBLE on the ESP32-C6
supports up to 4 simultaneous connections by default, so the OTA uploader can
connect alongside it; if the upload appears to hang, stop
`desktop-assistant-core.service` first to free up the connection slot and
rule out any BLE contention.
