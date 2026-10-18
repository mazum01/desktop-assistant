/*
 * VERA display firmware for Waveshare ESP32 1.47-inch LCD boards.
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
#include "display_board.h"
#include "eq_renderer.h"
#include "mouth_renderer.h"
#include "status_renderer.h"
#include "sd_video.h"
#include "orientation.h"
#include "media_config.h"
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <atomic>

namespace vera_display {

void initialize_hardware() {
  pinMode(vera_board::BACKLIGHT, OUTPUT);
  digitalWrite(vera_board::BACKLIGHT, LOW);
  vera_mouth::begin();
  vera_status::begin();
  vera_eq::begin();
  analogWrite(vera_board::BACKLIGHT, 255 * vera_board::BACKLIGHT_PERCENT / 100);
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
  kEq,
};

DisplayMode g_display_mode = DisplayMode::kMouth;

NimBLEServer *g_server = nullptr;
NimBLECharacteristic *g_command_char = nullptr;
NimBLECharacteristic *g_status_char = nullptr;
std::atomic<bool> g_connected{false};
std::atomic<uint16_t> g_connection_handle{BLE_HS_CONN_HANDLE_NONE};
std::atomic<bool> g_hello_pending{false};
std::atomic<bool> g_queue_overflow{false};
std::atomic<bool> g_message_overflow{false};
bool g_discard_message = false;
String g_rx_buffer;
struct PendingCommand { char line[vera_ble::MAX_MESSAGE_BYTES + 1]; };
QueueHandle_t g_commands = nullptr;
String g_request_id;
uint32_t g_footer_at = 0;
bool g_mouth_override = false;

NimBLEOta g_ota;

void send_status(JsonDocument &doc) {
  if (g_status_char == nullptr || !g_connected) {
    return;
  }
  String out;
  if (g_request_id.length()) doc["request_id"] = g_request_id;
  serializeJson(doc, out);
  out += '\n';
  uint16_t handle = g_connection_handle.load();
  uint16_t mtu = g_server->getPeerMTU(handle);
  size_t fragment = mtu > 23 ? min(size_t(244), size_t(mtu - 3)) : 20;
  for (size_t offset = 0; offset < out.length(); offset += fragment) {
    size_t count = min(fragment, out.length() - offset);
    if (!g_status_char->notify(
            reinterpret_cast<const uint8_t *>(out.c_str()) + offset, count, handle)) {
      Serial.println("VERA BLE status notification failed");
      break;
    }
    delay(20);
  }
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
  doc["board"] = vera_board::NAME;
  doc["touch"] = false;
  doc["video"] =
#if defined(VERA_DISPLAY_S3_TOUCH)
      "mjpeg";
#else
      "unsupported";
#endif
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
  g_request_id = "";
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
  g_request_id = String(doc["request_id"] | "").substring(0, 64);
  if (strcmp(cmd, "ping") == 0) {
    send_ack("ping", true);
    return;
  }
  if (strcmp(cmd, "video") == 0) {
    const char *action = doc["action"] | "";
    JsonDocument reply;
    reply["cmd"] = "video";
    reply["action"] = action;
    const char *error = nullptr;
    bool ok = true;
    if (strcmp(action, "play") == 0) {
      if (!doc["path"].is<const char *>() ||
          (doc["fps"].is<int>() == false && !doc["fps"].isNull()) ||
          (!doc["loop"].isNull() && !doc["loop"].is<bool>())) {
        ok = false;
        error = "invalid_video_arguments";
      } else {
        ok = vera_video::play(doc["path"], doc["fps"] | vera_media_config::DEFAULT_FPS,
                              doc["loop"] | false, &error);
        if (ok) {
          g_display_mode = DisplayMode::kMouth;
          g_mouth_override = false;
          vera_eq::clear();
        }
      }
    } else if (strcmp(action, "stop") == 0) {
      vera_video::stop();
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    } else if (strcmp(action, "list") == 0) {
      if (!doc["offset"].isNull() && !doc["offset"].is<unsigned>()) {
        ok = false;
        error = "invalid_list_offset";
      } else {
        ok = vera_video::list(reply.to<JsonObject>(), doc["offset"] | 0U, &error);
        reply["cmd"] = "video";
        reply["action"] = action;
      }
    } else if (strcmp(action, "status") != 0) {
      ok = false;
      error = "unknown_video_action";
    }
    reply["ok"] = ok;
    if (error) reply["error"] = error;
    vera_video::status(reply["video"].to<JsonObject>());
    vera_orientation::status(reply["imu"].to<JsonObject>());
    send_status(reply);
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
    g_mouth_override = state != vera_mouth::State::kNeutral;
    // Speech/emotion outranks the music visualization: drop the bars so the
    // mouth is visible immediately.
    vera_eq::clear();
    if (g_display_mode != DisplayMode::kMouth) {
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    }
    send_ack("mouth", true);
    return;
  }
  if (strcmp(cmd, "eq") == 0) {
    if (vera_video::active()) {
      send_ack("eq", true);
      return;
    }
    JsonArrayConst bins = doc["bins"].as<JsonArrayConst>();
    if (bins.isNull()) {
      send_ack("eq", false, "missing_bins");
      return;
    }
    // An empty array is the host's explicit "playback stopped" signal:
    // stop the visualization and hand the screen back to the mouth.
    if (bins.size() == 0) {
      vera_eq::clear();
      if (g_display_mode == DisplayMode::kEq) {
        g_display_mode = DisplayMode::kMouth;
        vera_mouth::force_redraw();
      }
      send_ack("eq", true);
      return;
    }
    float values[vera_eq::MAX_BANDS];
    size_t n = 0;
    for (JsonVariantConst v : bins) {
      if (n >= vera_eq::MAX_BANDS) {
        break;
      }
      values[n++] = v.as<float>();
    }
    vera_eq::set_bands(values, n);
    // A status message still outranks the visualization, so boot/error text
    // is never hidden by music bars.
    if (g_display_mode != DisplayMode::kStatus) {
      g_display_mode = DisplayMode::kEq;
    }
    send_ack("eq", true);
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
      if (g_discard_message) {
        g_discard_message = false;
        continue;
      }
      PendingCommand command{};
      g_rx_buffer.toCharArray(command.line, sizeof(command.line));
      if (xQueueSend(g_commands, &command, 0) != pdTRUE) {
        Serial.println("VERA display command queue full");
        g_queue_overflow = true;
        // The main loop owns drawing, SD, I2C and reply framing.
      }
      g_rx_buffer = "";
      continue;
    }
    if (g_discard_message) continue;
    if (g_rx_buffer.length() >= vera_ble::MAX_MESSAGE_BYTES) {
      // Drop an oversized/unterminated message to avoid unbounded growth
      // from a misbehaving or disconnected peer.
      g_rx_buffer = "";
      g_discard_message = true;
      g_message_overflow = true;
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
    g_connection_handle = connInfo.getConnHandle();
    g_connected = true;
    g_rx_buffer = "";
    g_discard_message = false;
    g_hello_pending = true;
  }

  void onDisconnect(NimBLEServer *server, NimBLEConnInfo &connInfo, int reason) override {
    g_connected = false;
    g_connection_handle = BLE_HS_CONN_HANDLE_NONE;
    g_rx_buffer = "";
    g_discard_message = false;
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
    vera_status::show(vera_status::Level::kProgress, msg);
    g_display_mode = DisplayMode::kStatus;
  }

  void onProgress(NimBLEOta *ota, uint32_t current, uint32_t total) override {
    char msg[32];
    int percent = total > 0 ? static_cast<int>((static_cast<uint64_t>(current) * 100) / total) : 0;
    snprintf(msg, sizeof(msg), "OTA update\n%d%%", percent);
    vera_status::show(vera_status::Level::kProgress, msg);
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
  Serial.printf("VERA display: %s; touch disabled\n", vera_board::NAME);
  vera_display::initialize_hardware();
  vera_orientation::begin();
  g_commands = xQueueCreate(12, sizeof(PendingCommand));
  if (!g_commands) {
    Serial.println("VERA display command queue allocation failed");
    while (true) delay(1000);
  }
  start_ble();
}

void loop() {
  if (g_hello_pending.exchange(false)) send_hello();
  if (g_queue_overflow.exchange(false) || g_message_overflow.load()) {
    JsonDocument reply;
    reply["event"] = "command_error";
    reply["ok"] = false;
    reply["error"] = g_message_overflow.exchange(false) ? "message_too_large" : "command_queue_full";
    send_status(reply);
  }
  PendingCommand command;
  for (int count = 0; count < 4 && xQueueReceive(g_commands, &command, 0) == pdTRUE; ++count) {
    handle_command(String(command.line));
    g_request_id = "";
  }
  vera_orientation::update();
  bool was_playing = vera_video::active();
  bool visible = g_display_mode == DisplayMode::kMouth && !g_mouth_override;
  bool video_frame = vera_video::update(visible);
  if (const char *error = vera_video::take_error()) {
    JsonDocument reply;
    reply["event"] = "video_error";
    reply["ok"] = false;
    reply["error"] = error;
    send_status(reply);
    vera_status::show(vera_status::Level::kError, error);
    g_display_mode = DisplayMode::kStatus;
  } else if (was_playing && !vera_video::active()) {
    JsonDocument reply;
    reply["event"] = "video_finished";
    reply["ok"] = true;
    send_status(reply);
    vera_mouth::force_redraw();
  }
  if (g_display_mode == DisplayMode::kStatus) {
    vera_status::update();
    if (vera_status::consume_ready_expired()) {
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    }
  } else if (g_display_mode == DisplayMode::kEq) {
    vera_eq::update();
    // The renderer self-clears when host frames stop arriving; follow it
    // back to the mouth so the panel never sits on a frozen spectrum.
    if (!vera_eq::is_active()) {
      g_display_mode = DisplayMode::kMouth;
      vera_mouth::force_redraw();
    }
  } else if (!vera_video::active() || g_mouth_override) {
    vera_mouth::update();
  }
  if (!video_frame && millis() - g_footer_at >= vera_media_config::FOOTER_PERIOD_MS) {
    g_footer_at = millis();
    vera_mouth::flush();
  }
  delay(20);
}
