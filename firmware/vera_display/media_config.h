#pragma once

#include <stddef.h>
#include <stdint.h>

namespace vera_media_config {
constexpr size_t MAX_JPEG_BYTES = 96 * 1024;
constexpr uint32_t MAX_FRAME_SCAN_MS = 100;
constexpr uint8_t DEFAULT_FPS = 10;
constexpr uint8_t MAX_FPS = 20;
constexpr int MAX_SOURCE_DIMENSION = 2560;
constexpr size_t MAX_PATH = 96;
constexpr size_t LIST_PAGE_SIZE = 4;
constexpr int SD_CLK = 16;
constexpr int SD_CMD = 15;
constexpr int SD_D0 = 17;
constexpr int IMU_SDA_S3 = 42;
constexpr int IMU_SCL_S3 = 41;
constexpr int IMU_SDA_C6 = 18;
constexpr int IMU_SCL_C6 = 19;
constexpr uint32_t IMU_PERIOD_MS = 20;
constexpr uint32_t IMU_STALE_MS = 500;
constexpr uint32_t I2C_CLOCK_HZ = 100000;
constexpr uint16_t I2C_TIMEOUT_MS = 20;
constexpr uint32_t I2C_SCAN_INTERVAL_MS = 30000;
constexpr uint8_t I2C_SCAN_FIRST = 0x08;
constexpr uint8_t I2C_SCAN_LAST = 0x77;
}  // namespace vera_media_config
