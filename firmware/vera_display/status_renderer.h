/*
 * VERA display startup/status renderer.
 *
 * Secondary display mode driven by BLE "status" commands (see
 * ble_protocol.h): concise text describing boot progress, per-service
 * readiness, ready/degraded/error conditions, and version announcements.
 * Renders on the same physical screen as mouth_renderer (via
 * vera_mouth::gfx()) but is mutually exclusive with it at any instant —
 * vera_display.ino switches which renderer's update() is driven based on
 * the most recently received command and an idle timeout.
 */

#pragma once

#include <Arduino.h>

namespace vera_status {

// Mirrors the small set of lifecycle states DisplayService emits (see
// src/services/display_service.py: boot, service_started, ready,
// degraded, version, status, service_stopped). Unrecognized/free-form
// state strings are rendered as plain text (kInfo) rather than rejected,
// since the host may add new state names over time.
enum class Level {
  kInfo,       // boot, service_started, service_stopped, version, generic status
  kReady,      // ready
  kDegraded,   // degraded
  kError,      // error
};

// Prepares any renderer-local state. Must be called once from setup(),
// after vera_mouth::begin() (status_renderer reuses that display
// instance rather than opening its own).
void begin();

// Displays a new status line. `message` is truncated/word-wrapped to fit
// the panel; repeated identical (level, message) pairs are coalesced to
// avoid redundant redraws. Also records the time of this call for the
// idle-timeout handled by vera_display.ino.
void show(Level level, const char *message);

// Drives redraw timing (e.g. clearing a transient "ready" flash after a
// hold period). Must be called frequently from loop() while status mode
// is active; internally rate-limits actual redraws.
void update();

// Returns true exactly once, the first update() call after a "ready"
// message's hold period has elapsed. vera_display.ino uses this as the
// signal to switch the active display mode back to the mouth renderer
// (transient statuses relinquish the screen; degraded/error persist
// until superseded by another status).
bool consume_ready_expired();

}  // namespace vera_status
