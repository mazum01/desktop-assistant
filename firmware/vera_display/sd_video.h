#pragma once

#include <ArduinoJson.h>

namespace vera_video {
bool play(const char *path, int fps, bool loop, const char **error);
void stop();
bool active();
bool update(bool visible);
const char *take_error();
void status(JsonObject out);
bool list(JsonObject out, unsigned offset, const char **error);
}  // namespace vera_video
