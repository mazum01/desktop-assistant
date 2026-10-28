#pragma once

#include <ArduinoJson.h>

namespace vera_orientation {
void begin();
void update();
void status(JsonObject out);
}  // namespace vera_orientation
