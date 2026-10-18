#include "sd_video.h"

#include <Arduino_GFX_Library.h>
#include <esp_heap_caps.h>
#if defined(VERA_DISPLAY_S3_TOUCH)
#include <JPEGDEC.h>
#include <SD_MMC.h>
#endif

#include "media_config.h"
#include "mouth_renderer.h"
#include "video_stream.h"

namespace vera_video {
namespace {
bool playing = false;
const char *last_error = nullptr;
String current_path;
uint32_t frame_count = 0;
#if defined(VERA_DISPLAY_S3_TOUCH)
File clip;
JPEGDEC decoder;
uint8_t *frame = nullptr;
bool looping = false, paused = false;
uint32_t next_frame = 0, period = 100;
uint8_t read_buffer[1024];
size_t read_pos = 0, read_count = 0;
uint32_t scan_ms = 0;
size_t scan_bytes = 0;

bool mount(const char **error) {
  if (SD_MMC.cardType() != CARD_NONE) return true;
  SD_MMC.end();
  if (!SD_MMC.setPins(vera_media_config::SD_CLK, vera_media_config::SD_CMD,
                      vera_media_config::SD_D0) || !SD_MMC.begin("/sdcard", true)) {
    *error = "sd_card_missing_or_mount_failed";
    return false;
  }
  return true;
}

bool valid_path(const char *path) {
  return vera_video_stream::valid_path(path, vera_media_config::MAX_PATH);
}

int draw_block(JPEGDRAW *block) {
  Arduino_GFX *gfx = vera_mouth::gfx();
  int content_h = gfx->height() - vera_media_config::FOOTER_HEIGHT;
  if (block->x < 0 || block->y < 0 || block->x + block->iWidth > gfx->width() ||
      block->y + block->iHeight > content_h) return 0;
  gfx->draw16bitRGBBitmap(block->x, block->y, block->pPixels,
                          block->iWidth, block->iHeight);
  return 1;
}

bool read_frame(size_t *length, const char **error) {
  uint32_t started = millis();
  scan_bytes = 0;
  vera_video_stream::FrameCollector collector;
  while (read_pos < read_count || clip.available()) {
    if (read_pos == read_count) {
      read_count = clip.read(read_buffer, sizeof(read_buffer));
      read_pos = 0;
      if (!read_count || read_count > sizeof(read_buffer)) {
        *error = "sd_read_failed";
        return false;
      }
    }
    int value = read_buffer[read_pos++];
    ++scan_bytes;
    scan_ms = millis() - started;
    auto result = collector.feed(value, frame, vera_media_config::MAX_JPEG_BYTES);
    if (result == vera_video_stream::FrameCollector::kOversized) {
      *error = "jpeg_frame_exceeds_96k";
      return false;
    }
    if (result == vera_video_stream::FrameCollector::kComplete) {
      *length = collector.length;
      return true;
    }
    if (millis() - started > vera_media_config::MAX_FRAME_SCAN_MS) {
      Serial.printf("VERA video read timeout: %u ms, %u JPEG bytes, file offset %u\n",
                    unsigned(millis() - started), unsigned(collector.length),
                    unsigned(clip.position()));
      *error = "frame_read_timeout";
      return false;
    }
  }
  if (collector.length) *error = "truncated_jpeg_frame";
  return false;
}
#endif
}  // namespace

void stop() {
  playing = false;
#if defined(VERA_DISPLAY_S3_TOUCH)
  clip.close();
  read_pos = read_count = 0;
  free(frame);
  frame = nullptr;
#endif
}

bool active() { return playing; }

bool play(const char *path, int fps, bool loop, const char **error) {
  stop();
  last_error = nullptr;
  frame_count = 0;
#if defined(VERA_DISPLAY_S3_TOUCH)
  if (!valid_path(path)) { *error = "use_/videos/name.mjpg_or_mjpeg"; return false; }
  if (fps < 1 || fps > vera_media_config::MAX_FPS) {
    *error = "fps_must_be_1_to_20";
    return false;
  }
  if (!mount(error)) return false;
  clip = SD_MMC.open(path, FILE_READ);
  if (!clip || clip.isDirectory()) {
    *error = "video_file_not_found";
    clip.close();
    return false;
  }
  frame = static_cast<uint8_t *>(heap_caps_malloc(vera_media_config::MAX_JPEG_BYTES,
                                                 MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (!frame) frame = static_cast<uint8_t *>(malloc(vera_media_config::MAX_JPEG_BYTES));
  if (!frame) { *error = "video_buffer_allocation_failed"; clip.close(); return false; }
  current_path = path;
  looping = loop;
  period = 1000 / fps;
  next_frame = millis();
  paused = false;
  playing = true;
  return true;
#else
  *error = "sd_video_requires_s3_touch_board";
  return false;
#endif
}

bool update(bool visible) {
#if defined(VERA_DISPLAY_S3_TOUCH)
  if (!playing) return false;
  if (!visible) { paused = true; return false; }
  uint32_t now = millis();
  if (paused) { next_frame = now; paused = false; }
  if (static_cast<int32_t>(now - next_frame) < 0) return false;
  next_frame = now + period;  // Never burst frames to catch up after a pause.
  size_t length = 0;
  const char *error = nullptr;
  bool read_ok = read_frame(&length, &error);
  if (!read_ok && !error && looping && frame_count > 0) {
    if (!clip.seek(0)) error = "sd_seek_failed";
    else {
      read_pos = read_count = 0;
      read_ok = read_frame(&length, &error);
    }
  }
  if (!read_ok) {
    last_error = error ? error : (frame_count ? nullptr : "no_jpeg_frames");
    stop();
    return false;
  }
  if (!decoder.openRAM(frame, length, draw_block)) {
    last_error = "unsupported_or_corrupt_jpeg";
    stop();
    return false;
  }
  int width = decoder.getWidth(), height = decoder.getHeight(), options = 0;
  Arduino_GFX *gfx = vera_mouth::gfx();
  int available_h = gfx->height() - vera_media_config::FOOTER_HEIGHT;
  int scale = vera_video_stream::scale_for(width, height, gfx->width(), available_h,
                                          vera_media_config::MAX_SOURCE_DIMENSION);
  if (!scale) {
    last_error = "jpeg_dimensions_exceed_scaling_limits";
    decoder.close();
    stop();
    return false;
  }
  if (scale == 2) options = JPEG_SCALE_HALF;
  if (scale == 4) options = JPEG_SCALE_QUARTER;
  if (scale == 8) options = JPEG_SCALE_EIGHTH;
  gfx->fillRect(0, 0, gfx->width(), available_h, RGB565_BLACK);
  decoder.setPixelType(RGB565_LITTLE_ENDIAN);
  bool ok = decoder.decode((gfx->width() - (width + scale - 1) / scale) / 2,
                           (available_h - (height + scale - 1) / scale) / 2, options);
  decoder.close();
  if (!ok) {
    last_error = "jpeg_decode_failed";
    stop();
    return false;
  }
  ++frame_count;
  vera_mouth::flush();
  return true;
#else
  return false;
#endif
}

const char *take_error() {
  const char *error = last_error;
  last_error = nullptr;
  return error;
}

void status(JsonObject out) {
  out["supported"] =
#if defined(VERA_DISPLAY_S3_TOUCH)
      true;
#else
      false;
#endif
  out["playing"] = playing;
  out["path"] = current_path;
  out["frames"] = frame_count;
  out["codec"] = "baseline_jpeg_mjpeg";
#if defined(VERA_DISPLAY_S3_TOUCH)
  out["sd_mounted"] = SD_MMC.cardType() != CARD_NONE;
  if (SD_MMC.cardType() != CARD_NONE) out["card_mb"] = SD_MMC.cardSize() / (1024 * 1024);
  out["frame_read_ms"] = scan_ms;
  out["frame_scan_bytes"] = scan_bytes;
#endif
}

bool list(JsonObject out, unsigned offset, const char **error) {
#if defined(VERA_DISPLAY_S3_TOUCH)
  if (!mount(error)) return false;
  File directory = SD_MMC.open("/videos");
  if (!directory || !directory.isDirectory()) {
    *error = "videos_directory_missing";
    return false;
  }
  JsonArray files = out["files"].to<JsonArray>();
  unsigned seen = 0, added = 0;
  File entry;
  while ((entry = directory.openNextFile())) {
    String path = String("/videos/") + entry.name();
    bool eligible = !entry.isDirectory() && valid_path(path.c_str());
    entry.close();
    if (!eligible || seen++ < offset) continue;
    if (added == vera_media_config::LIST_PAGE_SIZE) {
      out["next_offset"] = offset + added;
      break;
    }
    files.add(path);
    ++added;
  }
  directory.close();
  return true;
#else
  *error = "sd_video_requires_s3_touch_board";
  return false;
#endif
}
}  // namespace vera_video
