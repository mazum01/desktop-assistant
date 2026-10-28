# VERA Waveshare ESP32 LCD firmware

This Arduino sketch is the firmware project for the
[Waveshare ESP32-S3-Touch-LCD-1.47](https://www.waveshare.com/esp32-s3-touch-lcd-1.47.htm)
(default), with separate profiles for ESP32-C6-Touch-LCD-1.47 and the original
non-touch ESP32-C6-LCD-1.47.
The firmware runs on the ESP32; the Raspberry Pi host communicates with it
over BLE GATT.

## Board contract

- Default board: `ESP32-S3-Touch-LCD-1.47` (confirmed by USB chip identification)
- Display: 1.47-inch, 172x320 TFT, SPI; landscape rendering
- BLE GATT: identical protocol across all profiles
- S3 touch-board LCD pins: MOSI GPIO39, SCLK GPIO38, CS GPIO21, DC GPIO45,
  RST GPIO40; backlight GPIO46, driven with 50% PWM after initialization
- C6 touch-board LCD pins: MOSI GPIO2, SCLK GPIO1, CS GPIO14, DC GPIO15,
  RST GPIO22; backlight GPIO23
- Touch input: **disabled**; no touch driver, interrupts, reset, or polling.
  I2C is used only to probe an optional QMI8658 IMU; battery monitoring is unused.
- S3 microSD: SD_MMC 1-bit mode, CLK16/CMD15/D0=17; OPI PSRAM enabled.
- Legacy non-touch pins: MOSI GPIO6, SCLK GPIO7, CS GPIO14, DC GPIO15,
  RST GPIO21, backlight GPIO22

The shared pin contract is in `display_board.h`. Both touch boards use the
ST7789-compatible drawing interface with explicit 172x320 dimensions, 34-column
offsets, and the panel register sequence from Waveshare's
[official S3 demo](https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.47/ESP32-S3-Touch-LCD-1.47-Demo.zip),
cross-checked against its
[schematic](https://files.waveshare.com/wiki/ESP32-S3-Touch-LCD-1.47/ESP32-S3-Touch-LCD-1.47-Schematic.pdf).
The S3 LCD reset is GPIO40, not GPIO47 (touch reset). The vendor's simplest
HelloWorld example has an inconsistent constructor; the LVGL example uses
the correct GPIO40. VERA does not configure the touch reset.
The generic ST7789 initialization alone is not sufficient for this board.
Physical orientation, colors, and BLE operation still need verification on
each newly flashed board. The historical validation below covers the old board,
not the replacement touch model.

## Replacement S3 hardware validation (2026-10-17)

- USB chip identification: ESP32-S3, base MAC `28:84:85:88:1B:24`.
- New BLE address: `28:84:85:88:1B:25`, discovered over BLE rather than inferred.
- USB upload completed with flash hashes verified; BLE service and OTA service
  advertised, and ping, status, mouth, EQ, and neutral restoration acknowledged.
- Host `display.ble_address` now targets the replacement. Touch remains disabled.
- A labeled eight-mode RGB/orientation sweep identified mode 5 (MADCTL `0xA0`,
  landscape rotation 3, RGB) as correct on this panel. Canvas allocation skips
  physical output initialization so vendor registers are not reset afterward.
- Final production firmware was visually confirmed: red text on black,
  upright and readable left to right. VERA reconnected at the new BLE address.

## Historical hardware validation (physical Waveshare ESP32-C6-LCD-1.47)

Validated end-to-end against the physical board (firmware v1.53.1):

- **BLE discovery/pairing**: device advertises as `VERA-Display`
  (`AC:EB:E6:23:EC:76`); `DisplayService` on the host connects directly by
  address (no interactive pairing required) using `bleak`.
- **Previous host display (2026-10-13)**: the display connected to this host
  used `AC:EB:E6:3E:B6:E2`, before the S3 replacement.
  The address above belongs to the earlier hardware validation board.
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
mouth, status, EQ, and SD video renderers. The main loop owns ordinary command
processing, drawing, SD access, IMU updates, and BLE response framing.

`mouth_renderer.h`/`mouth_renderer.cpp` render the primary "mouth imitation"
display using `Arduino_GFX` (`Arduino_ESP32SPI` + `Arduino_ST7789`). Each
emotional state (`neutral`, `listening`, `speaking`, `happy`, `sad`,
`surprised`, `error`) maps to a small geometry/animation parameter set
(width/height fraction, corner radius, oscillation amplitude and period)
rather than a bitmap, so expressions are cheap to draw at ~15 fps and easy to
retune. The default neutral state is a thick, rounded bar with a subtle arc,
spanning 85% of the display width. An unrecognized state defaults to the
`error` rendering (distinct red color) rather than silently freezing on stale
content.

`libraries.txt` records the external Arduino library dependencies.

## Commanded microSD video (S3)

Supported codec/container: **baseline JPEG Motion JPEG in raw concatenated
JPEG streams**, lowercase `.mjpg` or `.mjpeg`, **without audio**. This is not
an AVI/MP4 demuxer: H.264, H.265, VP8/VP9, AV1, WebM, progressive JPEG, and
MJPEG inside AVI are not supported. Files already encoded in the supported
format play directly from the card; other originals require conversion elsewhere.
There is no autoplay and no video streaming over BLE.

Place clips in `/videos/` on a FAT32 microSD card, safely ejected from the computer.
Only filenames directly inside that directory are allowed (under 96 bytes total;
no subdirectories, `..`, or backslashes). No card formatting is attempted.

```bash
vera display video list
vera display video list --offset 4
vera display video play /videos/clip.mjpg --fps 10 --loop
vera display video status
vera display video stop
```

These commands wait for an actual device reply, with a 20-second timeout.
After restarting VERA, allow startup announcements and the BLE connection to
finish before issuing video commands; queued startup traffic can delay replies.
`list` returns at most four filenames and a `next_offset` when more are available.
`status` includes SD mount state, playback/frame counters, and IMU availability.
Playback starts only after `play`; opening a file is acknowledged before its
first frame is decoded, so subsequent decode errors appear as device events
and an on-screen error. A non-looping clip emits `video_finished` at EOF.
Speech/non-neutral mouth expressions and status messages pause video; neutral
resumes it once the status screen releases control. EQ cannot replace active video.

Video is centered on the full 320x172 screen. JPEGDEC selects
1/2/4/8 downscaling to fit without cropping or upscaling; clips too large to
fit at 1/8 scale fail explicitly. Each source dimension is limited to 2560
pixels, compressed frames to 96 KiB, and frame reads to 100 ms. Frame rate
is fixed by `--fps` (1-20, default 10), not file timestamps; decoding/SD/display
throughput may lower the achieved rate. Missed frames do not cause catch-up bursts.
For predictable results, encode directly to 320x172:

```bash
ffmpeg -i input.mp4 -an -vf 'scale=320:172:force_original_aspect_ratio=decrease,fps=10' \
  -c:v mjpeg -pix_fmt yuvj420p -q:v 7 -f mjpeg clip.mjpg
```

## IMU probe (status only)

Nothing about orientation is drawn on screen; the mouth, EQ, status text and
video use the full 172-pixel height. (Until v1.98.0 a 20-pixel roll/pitch/yaw
footer was reserved at the bottom. It was removed because the installed S3
board has no IMU; see *Root cause* below.)

The firmware still checks QMI8658 identity at addresses 0x6A/0x6B
(WHO_AM_I=0x05) before writing sensor configuration, and reports the result in
`vera display video status` under `imu`. C6 touch uses SDA18/SCL19; S3 uses
SDA42/SCL41. A failed identity probe does not prove the IMU is absent:
board revision, wiring, power, and bus faults can also prevent detection.
Legacy C6 reports the sensor unsupported. No magnetometer is used.

### I2C address polling

The display scans non-reserved 7-bit addresses **0x08-0x77** at startup and
30 seconds after each completed scan, on the configured pins at 100 kHz.
It probes one address per main-loop iteration, with a 20 ms transaction
timeout, using address-only transmissions (no register/configuration writes).
It does not poll the Raspberry Pi's I2C bus or guess alternative GPIOs.

Run `vera display video status` and inspect `imu.i2c`:

- `ready`, `sda`, `scl`, `clock_hz`: bus initialization and pin configuration.
- `scanning`, `completed_scans`, `scan_age_ms`: progress and age of the last
  completed scan. The first scan normally finishes within a few seconds.
- `addresses`: hexadecimal addresses that acknowledged in the current/latest
  scan. Results are partial while `scanning` is true; a new scan clears old ACKs.
- `errors`, `last_error_code`: failures other than normal address NACK (code 2),
  using Arduino Wire result codes; e.g. 5 indicates timeout.

An ACK identifies a responding address, **not the device model**. The scanner
does not configure unknown devices or enable touch input. QMI8658 initialization
still requires its documented address and identity check; discovery alone does
not reconfigure the IMU. Interrupt lines are not required for polled readings.

When present, accelerometer/gyro fusion reports roll, pitch, and **relative
yaw** in `status`, in degrees in sensor-board axes (not compensated for the robot mount).
Yaw starts at zero on initialization, drifts, and is **not compass heading**.
Gravity corrects roll/pitch only near 1g; Euler gyro updates are guarded near
the pitch singularity. Samples older than 500 ms are unavailable, and resuming
after a stale stream reinitializes orientation. With no detected IMU, status
omits numeric angles.

### Physical validation (2026-10-18)

- USB flash verified, S3 and C6 touch profiles compiled, and host CLI/BLE
  list/play/status/stop commands exercised against the inserted 29,620 MiB card.
- Generated 320x152 test stream looped past 60 frames; speech paused frame
  consumption and neutral resumed it. Non-looping playback finished at exactly
  20 frames; missing files returned explicit failures.
- The user confirmed the moving test pattern and correct orientation.
- The installed S3 returned `imu_not_detected` at both QMI8658 addresses; angle
  math is regression-tested, but physical sensor readings could not be tested
  because no supported onboard IMU was found.
- v1.74.0 completed repeated address scans on SDA42/SCL41 at 100 kHz: only
  `0x63` acknowledged, with no bus errors. Neither `0x6A` nor `0x6B` acknowledged.
  This establishes a responding device on that bus, not its identity or the
  absence of an IMU on a different board revision/pin assignment.

### Root cause (2026-10-27)

The ESP32-S3-Touch-LCD-1.47 **has no onboard IMU.** Waveshare's feature list
and demo set for the S3 board name only the JD9853 panel and the AXS5106L
touch controller. The `0x63` ACK is that touch IC (AXS5106L 7-bit address),
so `imu_not_detected` is correct. They do
not indicate a firmware or wiring fault.

The QMI8658A is fitted to the **ESP32-C6-Touch-LCD-1.47**, which shares the
AXS5106L on SDA GPIO18 / SCL GPIO19 (IMU INT1 GPIO5, INT2 GPIO6). These pins
match the C6 profile here. To get roll/pitch/yaw, either switch to the C6
touch board (build the C6 profile) or wire an external QMI8658 breakout to
the S3's I2C bus (SDA42/SCL41) at 0x6A/0x6B.

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

These commands now target the **S3 touch board with touch input disabled**.
Use `VERA_DISPLAY_BOARD=touch` for the C6 touch board.
For the original C6 non-touch board, set `VERA_DISPLAY_BOARD=legacy` on both build
and upload commands, including BLE OTA commands. Never send a touch-board
binary to the legacy board (or vice versa); their GPIO assignments differ.
Unknown profile names and mismatched chip overrides fail before building.
Build and USB upload share the same profile-to-chip selection.

Connect the replacement directly to the Pi with a USB data cable. Use
`arduino-cli board list` or `/dev/serial/by-id/` to identify its port; if needed,
hold BOOT while connecting/resetting to enter download mode. After USB upload,
verify that the screen displays the neutral mouth, then discover its new
`VERA-Display` BLE address and update `display.ble_address` on the host.
The previous board's address must not be reused. Restart core and verify
mouth, status, and EQ commands; the BLE protocol and OTA service are unchanged.
The BLE hello notification includes the board profile and `touch: false`.

The scripts keep Arduino CLI data and downloads under `firmware/` (ignored by
Git), install the ESP32 core and Arduino GFX dependency, and use
`esp32:esp32:esp32s3` by default (`esp32c6` for the C6 profiles).
Override board options or the CLI executable with `VERA_ESP32_FQBN` or
`ARDUINO_CLI` when needed; the FQBN must match the profile's chip. Upload requires a connected
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
./firmware/upload_esp32_display_ble.sh
```

This compiles the sketch, locates the resulting `.bin`, and streams it to the
device using `firmware/nimbleota_uploader.py` (a copy of NimBLEOta's
bundled uploader script;
chunked 4KB sectors with CRC16 verification and automatic
retry-on-error, per the NimBLEOta wire protocol). `VERA_ESP32_BLE_ADDRESS`
defaults to the address already configured in `config/assistant.yaml`
(`display.ble_address`). Because the default ESP32 partition table
(`Default 4MB with spiffs`) includes dual OTA app slots, no partition scheme
change is required.

**Automatic BLE link handover**: `DisplayService` on the host holds a
persistent BLE connection to the display. While it is connected the ESP32
stops advertising, so the uploader's scan-based connect fails with
`Device with address ... was not found`. The upload script therefore pauses
`desktop-assistant-core.service` (`kill -STOP`, since `systemctl stop` is not
in the passwordless sudo allowlist and killing it would trip
`Restart=on-failure`) and drops the link before uploading, then resumes the
service on exit — including on failure or `Ctrl-C`, via a shell `trap`.

**Build verification**: the firmware reports its compile timestamp as
`build` in the `hello` event and in `vera display video status`. After the
upload, the script resumes the core service and polls that status until
`build` matches the stamp embedded in the `.bin` (up to 60 s). It fails if
they don't match.

Before v1.98.0, the firmware rebooted inside NimBLEOta's completion callback.
That swallowed the final sector's ACK, so the uploader timed out on the last
sector even though the update had been applied. The reboot is now deferred to
`loop()`. If the final ACK is still missing, the uploader exits with code 3
and the script falls back to build verification instead of reporting failure.

### Script options

Both `build_esp32_display.sh` and `upload_esp32_display_ble.sh` print
numbered, timed steps. Useful environment variables:

| Variable | Effect |
| --- | --- |
| `VERA_VERBOSE=1` | Echo every command before running it, pass `-v` to `arduino-cli`, and print per-sector BLE debug output |
| `VERA_SKIP_BUILD=1` | Upload the existing `.bin` without recompiling (upload script only) |
| `VERA_ESP32_BLE_ADDRESS` | Target BLE MAC address |
| `VERA_ESP32_FQBN` | Override the board FQBN |
| `VERA_PYTHON` | Interpreter used to run the uploader |

The transfer reports live progress with throughput and ETA:

```
[##########--------------------]  34.4%  sector 67/195  12.4 KiB/s  ETA 0m41s
```

If you see `Could not acquire MTU ... using default 23` instead of
`Negotiated MTU: 517 bytes`, the transfer will still succeed but runs roughly
20x slower, since each chunk carries 17 bytes instead of 509.
