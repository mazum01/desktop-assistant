/*
 * VERA display firmware for the Waveshare ESP32-C6-LCD-1.47.
 *
 * This initial project scaffold owns the board/display contract. BLE command
 * handling and rendering are implemented in the subsequent LCD tasks.
 */

#include <Arduino.h>

namespace vera_display {

constexpr int LCD_MOSI = 6;
constexpr int LCD_SCLK = 7;
constexpr int LCD_CS = 14;
constexpr int LCD_DC = 15;
constexpr int LCD_RST = 21;
constexpr int LCD_BL = 22;
constexpr uint8_t MAX_BACKLIGHT_PERCENT = 50;

void initialize_hardware() {
  pinMode(LCD_BL, OUTPUT);
  digitalWrite(LCD_BL, LOW);
}

}  // namespace vera_display

void setup() {
  Serial.begin(115200);
  vera_display::initialize_hardware();
}

void loop() {
  delay(1000);
}
