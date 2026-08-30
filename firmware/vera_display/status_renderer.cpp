/*
 * VERA display startup/status renderer implementation.
 *
 * See status_renderer.h for the public contract. Draws simple wrapped
 * text on the shared display instance owned by mouth_renderer, using
 * plain Adafruit-GFX-compatible text primitives (no bitmap fonts/icons)
 * so it stays cheap and easy to extend.
 */

#include "status_renderer.h"

#include <Arduino_GFX_Library.h>

#include "mouth_renderer.h"

namespace vera_status {

namespace {

constexpr uint16_t COLOR_BG = RGB565_BLACK;
constexpr uint16_t COLOR_INFO = RGB565_WHITE;
constexpr uint16_t COLOR_READY = RGB565_LIME;
constexpr uint16_t COLOR_DEGRADED = RGB565_YELLOW;
constexpr uint16_t COLOR_ERROR = RGB565_RED;

// Text size multiplier for the built-in 6x8 glyph font; 2 keeps single
// words legible on a 172x320 panel without needing a custom font.
constexpr uint8_t TEXT_SIZE = 2;
constexpr int16_t GLYPH_W = 6 * TEXT_SIZE;
constexpr int16_t GLYPH_H = 8 * TEXT_SIZE;
constexpr int16_t LINE_SPACING = 4;
constexpr int16_t MARGIN = 8;

// "ready" is a transient flash: after this hold period with no new
// status, update() clears it back to blank so a one-time success message
// doesn't linger indefinitely and get mistaken for current state.
constexpr unsigned long READY_HOLD_MS = 4000;

Level g_level = Level::kInfo;
String g_message;
bool g_dirty = true;
bool g_ready_cleared = true;
bool g_ready_just_expired = false;
unsigned long g_shown_at_ms = 0;

uint16_t color_for(Level level) {
  switch (level) {
    case Level::kReady:
      return COLOR_READY;
    case Level::kDegraded:
      return COLOR_DEGRADED;
    case Level::kError:
      return COLOR_ERROR;
    case Level::kInfo:
    default:
      return COLOR_INFO;
  }
}

// Greedy word-wrap into fixed-width lines (in characters), matching the
// simple truncation approach DisplayService already uses on the host
// side (max_message_chars) rather than attempting sub-pixel layout.
void wrap_into_lines(const String &text, int max_chars_per_line, String lines[], int max_lines,
                      int *out_count) {
  int count = 0;
  int start = 0;
  int len = text.length();
  while (start < len && count < max_lines) {
    int remaining = len - start;
    int take = remaining < max_chars_per_line ? remaining : max_chars_per_line;
    int break_at = take;
    if (remaining > max_chars_per_line) {
      // Prefer breaking on the last space within the window so words
      // aren't split mid-token when it can be avoided.
      int last_space = text.lastIndexOf(' ', start + take);
      if (last_space > start) {
        break_at = last_space - start;
      }
    }
    lines[count++] = text.substring(start, start + break_at);
    start += break_at;
    while (start < len && text.charAt(start) == ' ') {
      ++start;  // skip the separating space
    }
  }
  if (count == max_lines && start < len) {
    // Mark truncation on the last visible line, mirroring the host's
    // "…" suffix convention (see DisplayService._emit_status).
    String &last = lines[max_lines - 1];
    if (last.length() > 1) {
      last = last.substring(0, last.length() - 1) + "\xE2\x80\xA6";  // UTF-8 ellipsis
    }
  }
  *out_count = count;
}

void draw(const char *message, uint16_t color) {
  Arduino_GFX *gfx = vera_mouth::gfx();
  if (gfx == nullptr) {
    return;
  }
  gfx->fillScreen(COLOR_BG);

  int16_t screen_w = gfx->width();
  int16_t screen_h = gfx->height();
  int max_chars_per_line = (screen_w - 2 * MARGIN) / GLYPH_W;
  if (max_chars_per_line < 4) {
    max_chars_per_line = 4;
  }
  int max_lines = (screen_h - 2 * MARGIN) / (GLYPH_H + LINE_SPACING);
  if (max_lines < 1) {
    max_lines = 1;
  }
  if (max_lines > 6) {
    max_lines = 6;  // keep status text compact even on a taller panel
  }

  static String lines[6];
  int line_count = 0;
  wrap_into_lines(String(message), max_chars_per_line, lines, max_lines, &line_count);

  gfx->setTextSize(TEXT_SIZE);
  gfx->setTextColor(color, COLOR_BG);
  gfx->setTextWrap(false);

  int16_t total_h = static_cast<int16_t>(line_count) * (GLYPH_H + LINE_SPACING) - LINE_SPACING;
  int16_t y = (screen_h - total_h) / 2;
  if (y < MARGIN) {
    y = MARGIN;
  }
  for (int i = 0; i < line_count; ++i) {
    int16_t text_w = static_cast<int16_t>(lines[i].length()) * GLYPH_W;
    int16_t x = (screen_w - text_w) / 2;
    if (x < MARGIN) {
      x = MARGIN;
    }
    gfx->setCursor(x, y);
    gfx->print(lines[i]);
    y += GLYPH_H + LINE_SPACING;
  }
}

}  // namespace

void begin() {
  g_level = Level::kInfo;
  g_message = "";
  g_dirty = false;
  g_ready_cleared = true;
  g_shown_at_ms = millis();
}

void show(Level level, const char *message) {
  String next(message == nullptr ? "" : message);
  if (next == g_message && level == g_level) {
    // Coalesce identical repeats but still refresh the hold timer so a
    // repeated "ready" doesn't get cleared out from under an unrelated
    // later idle check.
    g_shown_at_ms = millis();
    return;
  }
  g_level = level;
  g_message = next;
  g_shown_at_ms = millis();
  g_ready_cleared = false;
  g_dirty = true;
}

void update() {
  if (g_dirty) {
    draw(g_message.c_str(), color_for(g_level));
    g_dirty = false;
  }
  // Transient statuses (ready and info/version) expire after a hold period
  // so the display returns to the expressive mouth shape; warning/error
  // statuses persist until resolved or overridden.
  if ((g_level == Level::kReady || g_level == Level::kInfo) && !g_ready_cleared &&
      millis() - g_shown_at_ms >= READY_HOLD_MS) {
    g_ready_cleared = true;
    g_ready_just_expired = true;
  }
}

bool consume_ready_expired() {
  if (!g_ready_just_expired) {
    return false;
  }
  g_ready_just_expired = false;
  return true;
}

}  // namespace vera_status
