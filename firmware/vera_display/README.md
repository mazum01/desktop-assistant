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

## Project layout

`vera_display.ino` is intentionally a small, compilable board scaffold. The
BLE GATT protocol, mouth animation, and startup/status rendering are separate
implementation tasks so each can be tested independently.

`libraries.txt` records the external Arduino library dependency.

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
