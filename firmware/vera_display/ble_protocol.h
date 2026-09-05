/*
 * VERA display BLE GATT protocol definitions.
 *
 * The Raspberry Pi host (src/services/display_service.py) writes newline
 * (\n) terminated JSON command messages to the "command" characteristic.
 * The ESP32 notifies host-readable JSON status/ack messages on the
 * "status" characteristic. Both directions use plain UTF-8 JSON so either
 * side can be re-implemented independently as long as the framing and
 * field names below are preserved.
 *
 * Framing: BLE ATT payloads are limited by the negotiated MTU (typically
 * 20-244 bytes), so a single logical JSON message may be split across
 * several BLE writes/notifications. Each fragment is concatenated by the
 * receiver until a '\n' (0x0A) terminator is seen, at which point the
 * buffered bytes are parsed as one JSON object. This keeps both firmware
 * and host implementations simple (no explicit length-prefix framing is
 * required) while remaining robust to MTU negotiation differences.
 *
 * Command message shape (host -> device), one JSON object per line:
 *   {"cmd": "mouth", "state": "listening"}
 *   {"cmd": "status", "state": "boot", "message": "Display status service online"}
 *   {"cmd": "eq", "bins": [0.1, 0.8, 0.4, ...]}
 *   {"cmd": "ping"}
 *
 * The "eq" command drives the real-time graphic-EQ visualization: "bins" is
 * an ordered array of normalized 0.0-1.0 band levels (low frequency first),
 * produced by the host from the playback stream. An empty "bins" array means
 * "playback stopped" and returns the display to the mouth renderer. A "mouth"
 * or "status" command also takes the screen back, so speech and status text
 * are never hidden behind the visualization.
 *
 * Status/ack message shape (device -> host):
 *   {"ok": true, "cmd": "mouth"}
 *   {"ok": false, "cmd": "mouth", "error": "unknown_state"}
 *   {"event": "hello", "fw": "vera_display", "proto": 1}
 */

#pragma once

namespace vera_ble {

// Randomly generated, project-specific UUIDs (v4). These must match
// DisplayServiceConfig.ble_characteristic_uuid (command) and the
// corresponding status UUID on the host side.
constexpr const char *SERVICE_UUID = "f60ed61c-0f33-40db-893d-08ed0d7ad876";
constexpr const char *COMMAND_CHARACTERISTIC_UUID = "d741e8c9-f156-4c47-808f-f28ccd2760f2";
constexpr const char *STATUS_CHARACTERISTIC_UUID = "6a1e2c0a-6b8e-4a8a-9a7a-6c9c3e6b6a5e";

constexpr const char *DEVICE_NAME = "VERA-Display";

// Protocol version bumped whenever the message schema changes in a
// backward-incompatible way.
constexpr int PROTOCOL_VERSION = 1;

// Maximum buffered bytes for one in-flight (unterminated) JSON message.
// Guards against a disconnected/misbehaving peer growing the buffer
// unbounded; matches DisplayServiceConfig.max_message_chars headroom.
constexpr size_t MAX_MESSAGE_BYTES = 512;

}  // namespace vera_ble
