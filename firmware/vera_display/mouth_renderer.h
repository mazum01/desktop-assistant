/*
 * VERA display mouth renderer.
 *
 * Owns the ST7789 GFX display and draws a simple "mouth imitation" shape
 * whose geometry/animation conveys emotion, per the emotional states
 * driven by BLE "mouth" commands (see ble_protocol.h). This is the primary
 * display behavior for the Waveshare ESP32-C6-LCD-1.47.
 *
 * Rendering intentionally uses simple filled-shape primitives (no image
 * assets) so it stays cheap to draw at animation frame rates on this
 * microcontroller, and so the "expression" can be tuned by adjusting a
 * handful of geometry parameters per state instead of hand-drawing bitmaps.
 */

#pragma once

#include <Arduino.h>

namespace vera_mouth {

// Supported emotional states. UNKNOWN/ERROR both fall back to a safe,
// clearly-different neutral-ish rendering so a protocol mismatch or
// firmware bug is visually obvious without crashing or freezing on a
// stale frame.
enum class State {
  kNeutral,
  kListening,
  kSpeaking,
  kHappy,
  kSad,
  kSurprised,
  kError,
};

// Parses a BLE "mouth" command's state string. Returns false (leaving
// out unchanged) for anything not recognized, so callers can distinguish
// "unknown state" from a successful parse and ack accordingly.
bool parse_state(const char *name, State *out);

// Prepares the SPI bus/display and draws the initial neutral frame.
// Must be called once from setup() before update()/set_state().
void begin();

// Requests a new target state. Animation transitions/holds are handled
// internally by update(); this call is cheap and non-blocking.
void set_state(State state);

// Drives animation timing (e.g. blinking/talking motion, transition
// easing). Must be called frequently from loop(); internally rate-limits
// actual redraws so it is safe to call every loop iteration.
void update();

}  // namespace vera_mouth
