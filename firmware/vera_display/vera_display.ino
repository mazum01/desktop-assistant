/*
 * VERA display firmware for the Waveshare ESP32-C6-LCD-1.47.
 *
 * Owns the board/display pin contract plus the BLE GATT link to the
 * Raspberry Pi host (src/services/display_service.py). See
 * ble_protocol.h for the wire format. Mouth/startup rendering is
 * implemented in later firmware tasks; command parsing here already
 * recognizes "mouth" and "status" commands so those renderers can be
 * dropped in without touching the BLE plumbing.
 */

#include <Arduino.h>
#include <ArduinoJson.h>
#include <NimBLEDevice.h>

#include "ble_protocol.h"

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
  digitalWrite(LCD_BL, LOW);
}

}  // namespace vera_display

namespace {

NimBLEServer *g_server = nullptr;
NimBLECharacteristic *g_command_char = nullptr;
NimBLECharacteristic *g_status_char = nullptr;
bool g_connected = false;
String g_rx_buffer;

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
    // TODO(lcd-mouth-renderer): drive the emotional mouth animation state
    // machine. For now just acknowledge so the host protocol can be
    // validated end-to-end ahead of the renderer landing.
    const char *state = doc["state"] | "";
    if (strlen(state) == 0) {
      send_ack("mouth", false, "missing_state");
      return;
    }
    send_ack("mouth", true);
    return;
  }
  if (strcmp(cmd, "status") == 0) {
    // TODO(lcd-startup-renderer): render startup/status text or iconography.
    const char *state = doc["state"] | "";
    if (strlen(state) == 0) {
      send_ack("status", false, "missing_state");
      return;
    }
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

void start_ble() {
  NimBLEDevice::init(vera_ble::DEVICE_NAME);
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

  NimBLEAdvertising *advertising = NimBLEDevice::getAdvertising();
  advertising->addServiceUUID(vera_ble::SERVICE_UUID);
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
  delay(20);
}
