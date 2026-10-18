#include "orientation.h"

#include <Arduino_GFX_Library.h>
#include <Wire.h>
#include <math.h>

#include "media_config.h"
#include "mouth_renderer.h"
#include "orientation_math.h"

namespace vera_orientation {
namespace {
uint8_t address = 0;
const char *error = "imu_not_detected";
Angles angles;
uint32_t last_poll = 0, last_sample = 0;
bool configured = false;
bool bus_ready = false;
int sda = -1, scl = -1;
uint8_t scan_next = vera_media_config::I2C_SCAN_FIRST;
bool scan_active = true;
bool scan_ack[128] = {};
uint16_t scan_errors = 0;
uint8_t scan_last_error = 0;
uint32_t scan_completed = 0, scan_count = 0;

void scan_bus() {
  if (!bus_ready) return;
  if (!scan_active) {
    if (millis() - scan_completed < vera_media_config::I2C_SCAN_INTERVAL_MS) return;
    scan_active = true;
    scan_next = vera_media_config::I2C_SCAN_FIRST;
    memset(scan_ack, 0, sizeof(scan_ack));
    scan_errors = 0;
    scan_last_error = 0;
  }
  // One address per main-loop iteration, with no register/data writes.
  Wire.beginTransmission(scan_next);
  uint8_t result = Wire.endTransmission();
  scan_ack[scan_next] = result == 0;
  if (result != 0 && result != 2) {
    ++scan_errors;
    scan_last_error = result;
  }
  if (scan_next++ == vera_media_config::I2C_SCAN_LAST) {
    scan_active = false;
    scan_completed = millis();
    ++scan_count;
    Serial.printf("VERA I2C scan complete: SDA%d SCL%d, %u bus errors\n",
                  sda, scl, unsigned(scan_errors));
  }
}

bool read(uint8_t device, uint8_t reg, uint8_t *data, size_t count) {
  Wire.beginTransmission(device);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0) return false;
  if (Wire.requestFrom(device, count) != count) return false;
  for (size_t i = 0; i < count; ++i) data[i] = Wire.read();
  return true;
}

bool write(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(address);
  Wire.write(reg);
  Wire.write(value);
  return Wire.endTransmission() == 0;
}

bool available() {
  return address && angles.initialized && !error &&
         millis() - last_sample < vera_media_config::IMU_STALE_MS;
}
}  // namespace

void begin() {
#if defined(VERA_DISPLAY_S3_TOUCH)
  sda = vera_media_config::IMU_SDA_S3;
  scl = vera_media_config::IMU_SCL_S3;
#elif !defined(VERA_DISPLAY_LEGACY_LCD)
  sda = vera_media_config::IMU_SDA_C6;
  scl = vera_media_config::IMU_SCL_C6;
#else
  error = "imu_unsupported_board";
  return;
#endif
  if (!Wire.begin(sda, scl, vera_media_config::I2C_CLOCK_HZ)) {
    error = "i2c_init_failed";
    Serial.println("VERA I2C initialization failed");
    return;
  }
  bus_ready = true;
  Wire.setTimeOut(vera_media_config::I2C_TIMEOUT_MS);
  // Only enable a positively identified QMI8658; never write to a touch IC.
  for (uint8_t candidate : {uint8_t(0x6A), uint8_t(0x6B)}) {
    uint8_t identity = 0;
    if (read(candidate, 0, &identity, 1) && identity == 0x05) {
      address = candidate;
      break;
    }
  }
  if (!address) {
    Serial.println("VERA IMU: no QMI8658 detected; angles unavailable");
    return;
  }
  // Vendor register contract: auto-increment, +/-16g, +/-2048 dps, LPF.
  if (!write(0x60, 0xFF)) {
    error = "imu_reset_failed";
    return;
  }
  delay(100);
  if (!write(0x02, 0x40) || !write(0x03, 0x34) || !write(0x04, 0x74) ||
      !write(0x06, 0x55) || !write(0x08, 0x03)) {
    error = "imu_config_failed";
    return;
  }
  error = nullptr;
  configured = true;
  Serial.printf("VERA IMU: QMI8658 at 0x%02X\n", address);
}

void update() {
  scan_bus();
  uint32_t now = millis();
  if (!configured || now - last_poll < vera_media_config::IMU_PERIOD_MS) return;
  last_poll = now;
  uint8_t ready = 0, data[12];
  if (!read(address, 0x2E, &ready, 1)) {
    error = "imu_read_failed";
    return;
  }
  if ((ready & 3) != 3) return;
  if (!read(address, 0x35, data, sizeof(data))) {
    error = "imu_read_failed";
    return;
  }
  float v[6];
  for (int i = 0; i < 6; ++i) {
    int16_t raw = static_cast<int16_t>(uint16_t(data[i * 2]) |
                                     (uint16_t(data[i * 2 + 1]) << 8));
    v[i] = raw * (i < 3 ? 16.0f : 2048.0f) / 32768.0f;
  }
  angles.update(v, angles.initialized ? (now - last_sample) / 1000.0f : 0);
  last_sample = now;
  error = nullptr;
}

void draw() {
  Arduino_GFX *gfx = vera_mouth::gfx();
  if (!gfx) return;
  int y = gfx->height() - vera_media_config::FOOTER_HEIGHT;
  gfx->fillRect(0, y, gfx->width(), vera_media_config::FOOTER_HEIGHT, RGB565_BLACK);
  gfx->setTextSize(1);
  gfx->setTextWrap(false);
  gfx->setTextColor(available() ? RGB565_WHITE : RGB565_YELLOW);
  gfx->setCursor(4, y + 6);
  if (available()) {
    gfx->printf("R:%+5.1f P:%+5.1f Yrel:%+6.1f", angles.roll, angles.pitch, angles.yaw);
  } else {
    gfx->print(address ? "IMU unavailable / stale" : "R:-- P:-- Y:--  IMU not detected");
  }
}

void status(JsonObject out) {
  JsonObject bus = out["i2c"].to<JsonObject>();
  bus["ready"] = bus_ready;
  bus["sda"] = sda;
  bus["scl"] = scl;
  bus["clock_hz"] = vera_media_config::I2C_CLOCK_HZ;
  bus["scanning"] = bus_ready && scan_active;
  bus["completed_scans"] = scan_count;
  bus["scan_age_ms"] = scan_count ? millis() - scan_completed : 0;
  bus["errors"] = scan_errors;
  bus["last_error_code"] = scan_last_error;
  JsonArray addresses = bus["addresses"].to<JsonArray>();
  for (uint8_t device = vera_media_config::I2C_SCAN_FIRST;
       device <= vera_media_config::I2C_SCAN_LAST; ++device) {
    if (scan_ack[device]) {
      char hex[5];
      snprintf(hex, sizeof(hex), "0x%02X", device);
      addresses.add(hex);
    }
  }
  out["available"] = available();
  out["sensor"] = address ? "QMI8658" : "none";
  out["error"] = error ? error : (available() ? "" : "imu_stale");
  out["yaw_reference"] = "relative_startup_not_compass";
  if (available()) {
    out["roll"] = angles.roll;
    out["pitch"] = angles.pitch;
    out["yaw"] = angles.yaw;
  }
}
}  // namespace vera_orientation
