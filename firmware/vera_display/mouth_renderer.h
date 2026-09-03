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

// Forward-declared in the global namespace (matches Arduino_GFX_Library.h)
// so this header stays cheap to include; the concrete Arduino_GFX_Library.h
// is only pulled in by mouth_renderer.cpp and status_renderer.cpp (which
// shares the same display instance via vera_mouth::gfx()).
class Arduino_GFX;

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

// Forces the next update() call to fully clear and redraw the mouth,
// even if the animated bounding box hasn't changed. Needed after another
// renderer (e.g. status_renderer) has drawn over the whole screen, so the
// mouth's own erase-previous-rect optimization doesn't leave stale pixels.
void force_redraw();

// Returns the shared drawing surface (owned by this module) so other
// renderers (status_renderer) can draw on the same physical screen
// without opening a second SPI/display instance. Valid only after
// begin() has been called; returns nullptr otherwise.
//
// This is an offscreen canvas, not the physical display: all drawing
// (fillScreen, fillRoundRect, text, arcs, ...) happens into an in-RAM
// framebuffer. Nothing appears on the physical panel until flush() is
// called, which pushes the whole frame to the ST7789 in one SPI burst.
// This is what eliminates flicker/tearing from partially-clocked-out
// erase+redraw sequences -- the panel only ever shows a fully composited
// frame. Callers that draw via gfx() must call flush() once per frame
// after they're done drawing.
Arduino_GFX *gfx();

// Pushes the offscreen canvas contents to the physical display in one
// SPI transaction. Must be called once after any drawing via gfx()
// (mouth_renderer calls this itself from update(); status_renderer must
// call it after its own draw calls).
void flush();

}  // namespace vera_mouth

