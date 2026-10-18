#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

namespace vera_video_stream {
inline bool valid_path(const char *path, size_t max_path) {
  if (!path || strncmp(path, "/videos/", 8) != 0) return false;
  size_t length = strlen(path);
  if (length >= max_path || strchr(path + 8, '/') || strchr(path, '\\') ||
      strstr(path, "..")) return false;
  return (length > 13 && strcmp(path + length - 5, ".mjpg") == 0) ||
         (length > 14 && strcmp(path + length - 6, ".mjpeg") == 0);
}

inline int scale_for(int width, int height, int output_width, int output_height,
                     int max_dimension) {
  if (width <= 0 || height <= 0 || width > max_dimension || height > max_dimension) return 0;
  for (int scale = 1; scale <= 8; scale *= 2) {
    if ((width + scale - 1) / scale <= output_width &&
        (height + scale - 1) / scale <= output_height) return scale;
  }
  return 0;
}

struct FrameCollector {
  enum Result { kMore, kComplete, kOversized };
  size_t length = 0;
  int previous = -1;

  Result feed(uint8_t value, uint8_t *buffer, size_t capacity) {
    if (!length) {
      if (previous == 0xFF && value == 0xD8) {
        if (capacity < 2) return kOversized;
        buffer[0] = 0xFF;
        buffer[1] = 0xD8;
        length = 2;
      }
    } else {
      if (length == capacity) return kOversized;
      buffer[length++] = value;
      if (previous == 0xFF && value == 0xD9) return kComplete;
    }
    previous = value;
    return kMore;
  }
};
}  // namespace vera_video_stream
