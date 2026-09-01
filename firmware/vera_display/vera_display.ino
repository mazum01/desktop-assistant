/*
 * VERA display firmware for the Waveshare ESP32-C6-LCD-1.47.
 *
 * Owns the board/display pin contract plus the BLE GATT link to the
 * Raspberry Pi host (src/services/display_service.py). See
 * ble_protocol.h for the wire format. "mouth" commands drive
 * mouth_renderer (primary emotional expression); "status" commands drive
 * status_renderer (secondary boot/service/ready/degraded/error text).
 * The two renderers share one physical screen and are mutually exclusive
 * — see DisplayMode below.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <NimBLEDevice.h>
#include <NimBLEOta.h>

#include "ble_protocol.h"
#include "mouth_renderer.h"
#include "status_renderer.h"

namespace vera_display {

constexpr int LCD_MOSI = 6;
constexpr int LCD_SCLK = 7;
constexpr int LCD_CS = 14;
constexpr int LCD_DC = 15;
constexpr int LCD_RST = 21;
constexpr int LCD_BL = 22;
constexpr uint8_t MAX_BACKLIGHT_PERCENT = 50;

void initialize_hardware() {
  pinMode(LCD_BL, OUTPUT);
  // Backlight is driven digitally (on/off) rather than PWM-dimmed; the
  // Waveshare guidance to keep brightness <=50% is honored by the panel's
  // own default drive level, not by a duty cycle here.
  digitalWrite(LCD_BL, HIGH);
  vera_mouth::begin();
  vera_status::begin();
}

}  // namespace vera_display

namespace {

// The mouth (primary) and status (secondary) renderers share one
// physical screen and are mutually exclusive: whichever command arrived
// most recently owns the display. A status message holds the screen
// until superseded by another status, or (for the transient "ready"
// state only) until its hold period elapses, at which point control
// reverts to the mouth renderer.
enum class DisplayMode {
  kMouth,
  kStatus,
};

DisplayMode g_display_mode = DisplayMode::kMouth;

NimBLEServer *g_server = nullptr;
NimBLECharacteristic *g_command_char = nullptr;
NimBLECharacteristic *g_status_char = nullptr;
bool g_connected = false;
String g_rx_buffer;

NimBLEOta g_ota;

void send_status(JsonDocument &doc) {
  if (g_status_char == nullptr || !g_connected) {
    return;
  }
  String out;
  serializeJson(doc, out);
  out += '\n';
  g_status_char->setValue(reinterpret_cast<const uint8_t *>(out.c_str()), out.length());
  g_status_char->notify();
}

void send_ack(const char *cmd, bool ok, const char *error = nullptr) {
  JsonDocument doc;
  doc["ok"] = ok;
  doc["cmd"] = cmd;
  if (error != nullptr) {
    doc["error"] = error;
  }
  send_status(doc);
}

void send_hello() {
  JsonDocument doc;
  doc["event"] = "hello";
  doc["fw"] = "vera_display";
  doc["proto"] = vera_ble::PROTOCOL_VERSION;
  send_status(doc);
}

// Maps a DisplayService status "state" string (see
// src/services/display_service.py: boot, service_started,
// service_stopped, version, ready, degraded, status/generic, and
// upstream error topics normalized to "degraded") to a rendering level.
// Anything unrecognized renders as plain info text rather than being
// rejected, since the host may introduce new state names.
vera_status::Level level_for_state(const char *state_name) {
  if (strcmp(state_name, "ready") == 0) {
    return vera_status::Level::kReady;
  }
  if (strcmp(state_name, "degraded") == 0) {
    return vera_status::Level::kDegraded;
  }
  if (strcmp(state_name, "error") == 0) {
    return vera_status::Level::kError;
  }
  return vera_status::Level::kInfo;
}

// Dispatches one fully-buffered, newline-terminated JSON command. Unknown
// commands/states are ack'd with ok=false rather than dropped silently so
// the host can detect a protocol mismatch.
void handle_command(const String &line) {
  if (line.length() == 0) {
    return;
  }
  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) {
    send_ack("unknown", false, "invalid_json");
    return;
  }

  const char *cmd = doc["cmd"] | "";
  if (strcmp(cmd, "ping") == 0) {
    send_ack("ping", true);
    return;
  }
  if (strcmp(cmd, "mouth") == 0) {
    const char *state_name = doc["state"] | "";
    if (strlen(state_name) == 0) {
      send_ack("mouth", false, "missing_state");
      return;
    }
    vera_mouth::State state;
    if (!vera_mouth::parse_state(state_name, &state)) {
      send_ack("mouth", false, "unknown_state");
      return;
    }
    vera_mouth::set_state(state);
    if (g_display_mode != DisplayMode::kMouth) {
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    }
    send_ack("mouth", true);
    return;
  }
  if (strcmp(cmd, "status") == 0) {
    const char *state_name = doc["state"] | "";
    const char *message = doc["message"] | "";
    if (strlen(state_name) == 0) {
      send_ack("status", false, "missing_state");
      return;
    }
    vera_status::Level level = level_for_state(state_name);
    // Fall back to the state name itself when no message text was sent
    // (e.g. a bare {"cmd":"status","state":"ready"}), so the screen never
    // shows a blank status.
    vera_status::show(level, strlen(message) > 0 ? message : state_name);
    g_display_mode = DisplayMode::kStatus;
    send_ack("status", true);
    return;
  }

  send_ack(cmd, false, "unknown_cmd");
}


// Splits the growing receive buffer on '\n' terminators, dispatching each
// complete line. Handles BLE writes that fragment a single JSON message
// across multiple MTU-sized chunks, and multiple JSON messages arriving in
// one write.
void feed_command_bytes(const uint8_t *data, size_t len) {
  for (size_t i = 0; i < len; ++i) {
    char c = static_cast<char>(data[i]);
    if (c == '\n') {
      handle_command(g_rx_buffer);
      g_rx_buffer = "";
      continue;
    }
    if (g_rx_buffer.length() >= vera_ble::MAX_MESSAGE_BYTES) {
      // Drop an oversized/unterminated message to avoid unbounded growth
      // from a misbehaving or disconnected peer.
      g_rx_buffer = "";
      send_ack("unknown", false, "message_too_large");
      continue;
    }
    g_rx_buffer += c;
  }
}

class CommandCallbacks : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic *characteristic, NimBLEConnInfo &connInfo) override {
    const std::string &value = characteristic->getValue();
    if (!value.empty()) {
      feed_command_bytes(reinterpret_cast<const uint8_t *>(value.data()), value.size());
    }
  }
};

class ServerCallbacks : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer *server, NimBLEConnInfo &connInfo) override {
    g_connected = true;
    g_rx_buffer = "";
    send_hello();
  }

  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &connInfo, int reason) override {
    g_connected = false;
    g_rx_buffer = "";
    // Resume advertising so the host's DisplayService can reconnect after a
    // drop (sleep, out-of-range, host restart, etc.) without a firmware
    // reboot.
    NimBLEDevice::startAdvertising();
  }
};

// Reports BLE OTA firmware upload progress on-screen via the status
// renderer, reusing the existing "info"/"degraded"/"error" status levels
// so an OTA in progress is visible even if the host's DisplayService isn't
// actively driving normal mouth/status commands. The mouth renderer is
// intentionally left running its own update() as usual; switching to
// kStatus here just makes the OTA progress visible.
class OtaStatusCallbacks : public NimBLEOtaCallbacks {
  void onStart(NimBLEOta *ota, uint32_t firmwareSize, NimBLEOta::Reason reason) override {
    if (reason == NimBLEOta::Reconnected) {
      ota->stopAbortTimer();
    }
    char msg[48];
    snprintf(msg, sizeof(msg), "OTA update starting\n(%lu bytes)", static_cast<unsigned long>(firmwareSize));
    vera_status::show(vera_status::Level::kInfo, msg);
    g_display_mode = DisplayMode::kStatus;
  }

  void onProgress(NimBLEOta *ota, uint32_t current, uint32_t total) override {
    char msg[32];
    int percent = total > 0 ? static_cast<int>((static_cast<uint64_t>(current) * 100) / total) : 0;
    snprintf(msg, sizeof(msg), "OTA update\n%d%%", percent);
    vera_status::show(vera_status::Level::kInfo, msg);
  }

  void onStop(NimBLEOta *ota, NimBLEOta::Reason reason) override {
    if (reason == NimBLEOta::Disconnected) {
      // Give the host a window to reconnect and resume before giving up;
      // the OTA state (partial write handle) is preserved until then.
      ota->startAbortTimer(30);
      vera_status::show(vera_status::Level::kDegraded, "OTA paused\n(disconnected)");
      return;
    }
    if (reason == NimBLEOta::StopCmd) {
      ota->abortUpdate();
    }
    vera_status::show(vera_status::Level::kDegraded, "OTA update stopped");
  }

  void onComplete(NimBLEOta *ota) override {
    vera_status::show(vera_status::Level::kReady, "OTA complete\nRebooting...");
    delay(1500);
    ESP.restart();
  }

  void onError(NimBLEOta *ota, esp_err_t err, NimBLEOta::Reason reason) override {
    char msg[32];
    snprintf(msg, sizeof(msg), "OTA error %d", static_cast<int>(err));
    vera_status::show(vera_status::Level::kError, msg);
    if (reason == NimBLEOta::FlashError) {
      ota->abortUpdate();
    }
  }
} g_ota_callbacks;

void start_ble() {
  NimBLEDevice::init(vera_ble::DEVICE_NAME);
  // OTA firmware transfer needs a larger MTU than the default 23 bytes to
  // move 4KB sectors efficiently; this is safe to raise unconditionally
  // since our own command/status JSON messages already tolerate
  // fragmentation across MTU-sized chunks.
  NimBLEDevice::setMTU(517);
  g_server = NimBLEDevice::createServer();
  g_server->setCallbacks(new ServerCallbacks());

  NimBLEService *service = g_server->createService(vera_ble::SERVICE_UUID);

  g_command_char = service->createCharacteristic(
      vera_ble::COMMAND_CHARACTERISTIC_UUID,
      NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  g_command_char->setCallbacks(new CommandCallbacks());

  g_status_char = service->createCharacteristic(
      vera_ble::STATUS_CHARACTERISTIC_UUID,
      NIMBLE_PROPERTY::NOTIFY);

  service->start();

  // Separate GATT service (UUID 0x8018) for BLE firmware updates, so a
  // subsequent firmware build can be pushed over the same BLE radio
  // without a USB cable. See firmware/vera_display/README.md for the
  // host-side upload workflow.
  g_ota.start(&g_ota_callbacks);

  NimBLEAdvertising *advertising = NimBLEDevice::getAdvertising();
  advertising->addServiceUUID(vera_ble::SERVICE_UUID);
  advertising->addServiceUUID(g_ota.getServiceUUID());
  advertising->setName(vera_ble::DEVICE_NAME);
  advertising->start();
}

}  // namespace

void setup() {
  Serial.begin(115200);
  vera_display::initialize_hardware();
  start_ble();
}

void loop() {
  if (g_display_mode == DisplayMode::kStatus) {
    vera_status::update();
    if (vera_status::consume_ready_expired()) {
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    }
  } else {
    vera_mouth::update();
  }
  delay(20);
}
