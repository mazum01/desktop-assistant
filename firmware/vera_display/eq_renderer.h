/*
 * VERA display graphic-EQ (spectrum) renderer.
 *
 * Draws a real-time bar-graph visualization of whatever audio is currently
 * playing (music, podcasts). Band levels arrive from the host over BLE as
 * {"cmd":"eq","bins":[...]} messages -- see ble_protocol.h -- computed by
 * src/services/playback_spectrum_service.py from the PipeWire EQ sink's
 * monitor, so the bars track the actual post-EQ speaker signal.
 *
 * Like the mouth and status renderers, all drawing goes into the shared
 * offscreen canvas owned by mouth_renderer (vera_mouth::gfx()) and is pushed
 * to the panel with vera_mouth::flush(), so frames never tear.
 *
 * The renderer keeps its own decay animation: BLE frames arrive at ~12fps but
 * the display refreshes faster, and bars that fall smoothly between updates
 * look far better than ones that step. If host frames stop arriving entirely
 * the bars decay to zero and the visualization yields back to the mouth,
 * rather than freezing on a stale spectrum.
 */

#pragma once

#include <Arduino.h>

namespace vera_eq {

// Maximum bands the renderer will display. Host sends 12 by default; extra
// bands beyond this are ignored rather than overflowing the buffer.
constexpr size_t MAX_BANDS = 24;

// Milliseconds without a host frame before the visualization is considered
// stale and yields the display back to the mouth renderer. Must comfortably
// exceed the host frame interval (~83ms at 12fps) so ordinary BLE jitter
// doesn't cause flapping.
constexpr unsigned long STALE_TIMEOUT_MS = 1200;

// Prepares internal state. Safe to call before vera_mouth::begin(); no
// drawing happens until update().
void begin();

// Feeds a new spectrum frame (values clamped to 0.0..1.0). Cheap and
// non-blocking: rendering happens in update().
void set_bands(const float *values, size_t count);

// True while recent host frames make the visualization the active display
// owner. vera_display.ino uses this to decide whether to drive the EQ
// renderer or the mouth renderer.
bool is_active();

// Stops the visualization immediately (e.g. playback ended, or speech takes
// over the display) and hands the screen back to the mouth renderer.
void clear();

// Draws one animated frame if due. Call frequently from loop(); internally
// rate-limited, and a no-op when not active.
void update();

}  // namespace vera_eq
