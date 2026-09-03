/*
 * VERA display mouth renderer implementation.
 *
 * See mouth_renderer.h for the public contract. Uses Arduino_GFX
 * (Arduino_ESP32SPI + Arduino_ST7789) per the Waveshare-documented pin
 * mapping for the ESP32-C6-LCD-1.47.
 */

#include "mouth_renderer.h"

#include <Arduino_GFX_Library.h>

namespace vera_mouth {

namespace {

// Pin mapping matches vera_display.ino's board contract
// (firmware/vera_display/README.md).
constexpr int PIN_MOSI = 6;
constexpr int PIN_SCLK = 7;
constexpr int PIN_CS = 14;
constexpr int PIN_DC = 15;
constexpr int PIN_RST = 21;

// Landscape rotation so the mouth shape reads naturally wider than tall.
constexpr uint8_t DISPLAY_ROTATION = 1;

constexpr uint16_t COLOR_BG = RGB565_BLACK;
constexpr uint16_t COLOR_MOUTH = RGB565_CYAN;  // calm/friendly "face" accent
constexpr uint16_t COLOR_ERROR = RGB565_RED;

// One redraw per period keeps animation smooth without saturating the
// SPI bus / burning CPU on a microcontroller shared with BLE handling.
constexpr unsigned long FRAME_PERIOD_MS = 66;  // ~15 fps

Arduino_DataBus *g_bus = nullptr;
Arduino_GFX *g_panel = nullptr;   // physical ST7789 (SPI output target)
Arduino_GFX *g_gfx = nullptr;     // offscreen canvas all drawing goes through

State g_current_state = State::kNeutral;
unsigned long g_last_frame_ms = 0;
unsigned long g_state_started_ms = 0;
// Previous drawn rect, so each frame only needs to erase-and-redraw the
// mouth region instead of clearing the whole screen (reduces flicker and
// SPI traffic).
int16_t g_prev_x = 0, g_prev_y = 0, g_prev_w = 0, g_prev_h = 0;
bool g_have_prev = false;

// Shape "family" — how the mouth is actually drawn, independent of its
// animated size. Curve-based shapes use Arduino_GFX::fillArc() to draw a
// genuine smile/frown arc instead of an abstract rounded rectangle.
enum class Shape {
  kBar,     // flat/rounded bar (neutral, listening, error)
  kSmile,   // upward curve, like a smiling mouth (happy)
  kFrown,   // downward curve (sad)
  kOval,    // filled open-mouth ellipse (speaking, surprised)
};

struct Geometry {
  Shape shape;
  // Mouth bounding box is expressed as a fraction of display width/height
  // so it scales correctly regardless of rotation.
  float width_frac;
  float height_frac;
  // kBar only: corner radius fraction (of half-height).
  float radius_frac;
  // kSmile/kFrown only: how deep the curve bulges, as a fraction of the
  // bounding box height. 0 = flat line, 1 = a full half-circle.
  float curve_frac;
  // Animation amplitude: how much height_frac oscillates per animation
  // cycle. 0 = static.
  float bounce_frac;
  // Animation period in milliseconds for one full oscillation.
  unsigned long period_ms;
  uint16_t color;
};

const Geometry &geometry_for(State state) {
  static const Geometry kNeutral{Shape::kBar, 0.5f, 0.10f, 0.5f, 0.0f, 0.0f, 1000, COLOR_MOUTH};
  static const Geometry kListening{Shape::kBar, 0.45f, 0.16f, 0.6f, 0.0f, 0.15f, 900, COLOR_MOUTH};
  // Speaking previously bounced from 0.22 to 0.77 height every 260ms, which
  // reads as flicker rather than a talking motion at 15fps. Reduced
  // amplitude and slowed the period so the oval's size changes are visible
  // as smooth pulsing across several frames instead of a jarring jump.
  static const Geometry kSpeaking{Shape::kOval, 0.55f, 0.28f, 0.0f, 0.0f, 0.22f, 420, COLOR_MOUTH};
  static const Geometry kHappy{Shape::kSmile, 0.62f, 0.34f, 0.0f, 0.6f, 0.05f, 1400, COLOR_MOUTH};
  static const Geometry kSad{Shape::kFrown, 0.5f, 0.28f, 0.0f, 0.45f, 0.0f, 1000, COLOR_MOUTH};
  static const Geometry kSurprised{Shape::kOval, 0.32f, 0.32f, 0.0f, 0.0f, 0.08f, 500, COLOR_MOUTH};
  static const Geometry kError{Shape::kBar, 0.5f, 0.08f, 0.0f, 0.0f, 0.0f, 1000, COLOR_ERROR};

  switch (state) {
    case State::kNeutral:
      return kNeutral;
    case State::kListening:
      return kListening;
    case State::kSpeaking:
      return kSpeaking;
    case State::kHappy:
      return kHappy;
    case State::kSad:
      return kSad;
    case State::kSurprised:
      return kSurprised;
    case State::kError:
    default:
      return kError;
  }
}

// Computes the current animated bounding box for the active state, given
// elapsed time since the state was entered.
void compute_frame_rect(const Geometry &geo, unsigned long elapsed_ms, int16_t *x, int16_t *y,
                         int16_t *w, int16_t *h) {
  int16_t screen_w = g_gfx->width();
  int16_t screen_h = g_gfx->height();

  float phase = 0.0f;
  if (geo.period_ms > 0 && geo.bounce_frac > 0.0f) {
    phase = (sinf(2.0f * PI * static_cast<float>(elapsed_ms % geo.period_ms) /
                  static_cast<float>(geo.period_ms)) +
              1.0f) *
            0.5f;  // 0..1
  }

  float height_frac = geo.height_frac + geo.bounce_frac * phase;
  float width = geo.width_frac * static_cast<float>(screen_w);
  float height = height_frac * static_cast<float>(screen_h);

  *w = static_cast<int16_t>(width);
  *h = static_cast<int16_t>(height);
  *x = static_cast<int16_t>((screen_w - width) / 2.0f);
  *y = static_cast<int16_t>((screen_h - height) / 2.0f);
}

// Draws a smile/frown as a thick curved stroke (a parabolic arc sampled as
// overlapping filled circles), rather than an abstract rounded rectangle.
// `bulge_down` true draws a smile (curves downward like a "U", i.e. corners
// turn up); false draws a frown (curves upward, corners turn down).
void draw_curve(int16_t x, int16_t y, int16_t w, int16_t h, float curve_frac, bool bulge_down,
                 uint16_t color) {
  int16_t cx = x + w / 2;
  int16_t cy = y + h / 2;
  float half_w = w / 2.0f;
  float bulge = curve_frac * static_cast<float>(h) * (bulge_down ? 1.0f : -1.0f);
  // Stroke thickness scales with the box height so the curve reads as a
  // mouth outline rather than a hairline, and never drops below something
  // visible on a 172x320 panel.
  int16_t thickness = static_cast<int16_t>(h * 0.32f);
  if (thickness < 4) {
    thickness = 4;
  }
  int16_t stroke_r = thickness / 2;

  constexpr int kSegments = 18;
  for (int i = 0; i <= kSegments; ++i) {
    float t = -1.0f + 2.0f * static_cast<float>(i) / static_cast<float>(kSegments);
    int16_t px = cx + static_cast<int16_t>(t * half_w);
    int16_t py = cy + static_cast<int16_t>(bulge * (1.0f - t * t));
    g_gfx->fillCircle(px, py, stroke_r, color);
  }
}

void draw_frame(bool force) {
  if (g_gfx == nullptr) {
    return;
  }
  const Geometry &geo = geometry_for(g_current_state);
  unsigned long elapsed = millis() - g_state_started_ms;

  int16_t x, y, w, h;
  compute_frame_rect(geo, elapsed, &x, &y, &w, &h);

  if (!force && g_have_prev && x == g_prev_x && y == g_prev_y && w == g_prev_w && h == g_prev_h) {
    return;
  }

  // Drawing happens entirely in the offscreen canvas (RAM), so there's no
  // SPI cost to redrawing the whole frame instead of erasing just the
  // previous rect -- and it removes the erase/redraw race that caused
  // visible artifacts when a frame was drawn out of sync with the panel.
  g_gfx->fillScreen(COLOR_BG);

  switch (geo.shape) {
    case Shape::kSmile:
      draw_curve(x, y, w, h, geo.curve_frac, /*bulge_down=*/true, geo.color);
      break;
    case Shape::kFrown:
      draw_curve(x, y, w, h, geo.curve_frac, /*bulge_down=*/false, geo.color);
      break;
    case Shape::kOval:
      g_gfx->fillEllipse(x + w / 2, y + h / 2, w / 2, h / 2, geo.color);
      break;
    case Shape::kBar:
    default: {
      int16_t radius = static_cast<int16_t>((h / 2) * geo.radius_frac);
      g_gfx->fillRoundRect(x, y, w, h, radius, geo.color);
      break;
    }
  }

  g_prev_x = x;
  g_prev_y = y;
  g_prev_w = w;
  g_prev_h = h;
  g_have_prev = true;
}

}  // namespace

bool parse_state(const char *name, State *out) {
  if (name == nullptr || out == nullptr) {
    return false;
  }
  if (strcmp(name, "neutral") == 0) {
    *out = State::kNeutral;
  } else if (strcmp(name, "listening") == 0) {
    *out = State::kListening;
  } else if (strcmp(name, "speaking") == 0) {
    *out = State::kSpeaking;
  } else if (strcmp(name, "happy") == 0) {
    *out = State::kHappy;
  } else if (strcmp(name, "sad") == 0) {
    *out = State::kSad;
  } else if (strcmp(name, "surprised") == 0) {
    *out = State::kSurprised;
  } else if (strcmp(name, "error") == 0) {
    *out = State::kError;
  } else {
    return false;
  }
  return true;
}

void begin() {
  g_bus = new Arduino_ESP32SPI(PIN_DC, PIN_CS, PIN_SCLK, PIN_MOSI, -1 /* MISO unused */);
  g_panel = new Arduino_ST7789(g_bus, PIN_RST, DISPLAY_ROTATION, true /* IPS panel */);
  g_panel->begin();

  // All drawing (mouth + status renderer) happens into this offscreen
  // canvas; nothing reaches the panel until flush() below. A full
  // 172x320x16bpp frame is ~110KB, comfortably inside the ESP32-C6's free
  // heap (~300KB with this sketch), so buffering the whole screen is
  // simpler and cheaper than a partial/dirty-rect scheme.
  g_gfx = new Arduino_Canvas(g_panel->width(), g_panel->height(), g_panel);
  g_gfx->begin();
  g_gfx->fillScreen(COLOR_BG);

  g_current_state = State::kNeutral;
  g_state_started_ms = millis();
  g_have_prev = false;
  draw_frame(/*force=*/true);
  flush();
}

void set_state(State state) {
  if (state == g_current_state) {
    return;
  }
  g_current_state = state;
  g_state_started_ms = millis();
}

void update() {
  if (g_gfx == nullptr) {
    return;
  }
  unsigned long now = millis();
  if (now - g_last_frame_ms < FRAME_PERIOD_MS) {
    return;
  }
  g_last_frame_ms = now;
  draw_frame(/*force=*/false);
  flush();
}

void force_redraw() {
  g_have_prev = false;
}

Arduino_GFX *gfx() {
  return g_gfx;
}

void flush() {
  if (g_gfx == nullptr) {
    return;
  }
  // static_cast is safe: g_gfx is always constructed as an Arduino_Canvas
  // in begin(); the base-class pointer is only for API cleanliness.
  static_cast<Arduino_Canvas *>(g_gfx)->flush();
}

}  // namespace vera_mouth
