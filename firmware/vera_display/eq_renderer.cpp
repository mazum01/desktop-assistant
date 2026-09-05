/*
 * VERA display graphic-EQ renderer implementation.
 *
 * See eq_renderer.h for the public contract.
 */

#include "eq_renderer.h"

#include <Arduino_GFX_Library.h>

#include "mouth_renderer.h"

namespace vera_eq {

namespace {

constexpr uint16_t COLOR_BG = RGB565_BLACK;

// Redraw cadence. Faster than the host's ~12fps frame rate so the local
// decay animation interpolates smoothly between received frames.
constexpr unsigned long FRAME_PERIOD_MS = 40;  // ~25 fps

// Per-frame fall applied between host updates. Bars only ever decay locally;
// rises come from actual host data, so a dropped BLE frame can never fake
// a peak.
constexpr float LOCAL_DECAY = 0.06f;

// Peak-hold marker: a thin line that sits at each band's recent maximum and
// sinks slowly, the classic hardware-EQ look. Makes transients readable at a
// glance even when the bars themselves move quickly.
constexpr float PEAK_DECAY = 0.015f;

float g_levels[MAX_BANDS];
float g_peaks[MAX_BANDS];
size_t g_count = 0;
unsigned long g_last_frame_ms = 0;
unsigned long g_last_data_ms = 0;
bool g_active = false;
bool g_needs_clear = false;

// Bars are colored by height rather than by band, so loud passages visibly
// "heat up" from green through amber to red -- readable at a glance on a
// small panel where individual band positions are hard to judge.
uint16_t color_for(float level) {
  if (level >= 0.85f) {
    return RGB565_RED;
  }
  if (level >= 0.65f) {
    return RGB565_ORANGE;
  }
  if (level >= 0.40f) {
    return RGB565_YELLOW;
  }
  return RGB565_GREEN;
}

void draw_frame() {
  Arduino_GFX *gfx = vera_mouth::gfx();
  if (gfx == nullptr || g_count == 0) {
    return;
  }

  const int16_t w = gfx->width();
  const int16_t h = gfx->height();

  gfx->fillScreen(COLOR_BG);

  // Leave a small margin so bars don't bleed into the panel bezel.
  const int16_t margin_x = 4;
  const int16_t margin_y = 6;
  const int16_t usable_w = w - (margin_x * 2);
  const int16_t usable_h = h - (margin_y * 2);
  if (usable_w <= 0 || usable_h <= 0) {
    return;
  }

  const int16_t slot = usable_w / static_cast<int16_t>(g_count);
  if (slot <= 0) {
    return;
  }
  // One pixel of breathing room between bars, but never a zero-width bar.
  int16_t bar_w = slot - 2;
  if (bar_w < 1) {
    bar_w = slot;
  }

  for (size_t i = 0; i < g_count; ++i) {
    float level = g_levels[i];
    if (level < 0.0f) level = 0.0f;
    if (level > 1.0f) level = 1.0f;

    int16_t bar_h = static_cast<int16_t>(level * usable_h);
    const int16_t x = margin_x + static_cast<int16_t>(i) * slot;

    // Always show a baseline pixel row so silent bands still read as bars
    // rather than the display looking broken/blank.
    if (bar_h < 1) {
      bar_h = 1;
    }
    const int16_t y = margin_y + usable_h - bar_h;
    gfx->fillRect(x, y, bar_w, bar_h, color_for(level));

    // Peak-hold marker.
    float peak = g_peaks[i];
    if (peak > 0.02f) {
      if (peak > 1.0f) peak = 1.0f;
      const int16_t py =
          margin_y + usable_h - static_cast<int16_t>(peak * usable_h);
      gfx->drawFastHLine(x, py, bar_w, RGB565_WHITE);
    }
  }

  vera_mouth::flush();
}

}  // namespace

void begin() {
  for (size_t i = 0; i < MAX_BANDS; ++i) {
    g_levels[i] = 0.0f;
    g_peaks[i] = 0.0f;
  }
  g_count = 0;
  g_active = false;
  g_needs_clear = false;
}

void set_bands(const float *values, size_t count) {
  if (values == nullptr || count == 0) {
    return;
  }
  if (count > MAX_BANDS) {
    count = MAX_BANDS;
  }

  // A changed band count means a different visualization layout; reset so
  // stale bars from the old layout can't linger.
  if (count != g_count) {
    for (size_t i = 0; i < MAX_BANDS; ++i) {
      g_levels[i] = 0.0f;
      g_peaks[i] = 0.0f;
    }
    g_count = count;
  }

  for (size_t i = 0; i < count; ++i) {
    float v = values[i];
    if (v < 0.0f) v = 0.0f;
    if (v > 1.0f) v = 1.0f;
    // Host frames drive rises directly; local decay handles the fall so
    // motion stays smooth between updates.
    if (v > g_levels[i]) {
      g_levels[i] = v;
    }
    if (v > g_peaks[i]) {
      g_peaks[i] = v;
    }
  }

  g_last_data_ms = millis();
  if (!g_active) {
    g_active = true;
    // Taking over from the mouth/status renderer: force a full repaint so
    // their pixels can't show through the bar gaps.
    g_needs_clear = true;
  }
}

bool is_active() {
  return g_active;
}

void clear() {
  if (!g_active) {
    return;
  }
  g_active = false;
  g_count = 0;
  for (size_t i = 0; i < MAX_BANDS; ++i) {
    g_levels[i] = 0.0f;
    g_peaks[i] = 0.0f;
  }
  // The mouth renderer's erase-previous-rect optimization assumes it owns
  // the screen, so tell it to repaint fully after we've overwritten it.
  vera_mouth::force_redraw();
}

void update() {
  if (!g_active) {
    return;
  }

  const unsigned long now = millis();

  // Host stopped sending (playback ended, BLE dropped): yield the display
  // rather than freezing on a stale spectrum.
  if (now - g_last_data_ms > STALE_TIMEOUT_MS) {
    clear();
    return;
  }

  if (!g_needs_clear && (now - g_last_frame_ms < FRAME_PERIOD_MS)) {
    return;
  }
  g_last_frame_ms = now;
  g_needs_clear = false;

  for (size_t i = 0; i < g_count; ++i) {
    g_levels[i] -= LOCAL_DECAY;
    if (g_levels[i] < 0.0f) {
      g_levels[i] = 0.0f;
    }
    g_peaks[i] -= PEAK_DECAY;
    if (g_peaks[i] < 0.0f) {
      g_peaks[i] = 0.0f;
    }
  }

  draw_frame();
}

}  // namespace vera_eq
