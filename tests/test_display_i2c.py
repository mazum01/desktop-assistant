"""Exercise the actual firmware scanner with a simulated Wire bus."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("init_ok", [True, False])
def test_i2c_scan_bounds_acknowledgments_errors_and_interval(tmp_path, init_ok):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ required for firmware tests")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "Arduino.h").write_text(
        """
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
inline uint32_t now = 0;
inline uint32_t millis() { return now; }
inline void delay(int ms) { now += ms; }
struct SerialStub {
  void println(const char*) {}
  template<class... T> void printf(const char*, T...) {}
};
inline SerialStub Serial;
"""
    )
    (tmp_path / "Wire.h").write_text(
        """
#pragma once
#include "Arduino.h"
#include <vector>
struct WireStub {
  bool init_ok = INIT_OK, present = true;
  int device = -1, data_count = 0, sda = -1, scl = -1, hz = 0, timeout = 0;
  std::vector<int> probes;
  bool begin(int a, int b, int c) { sda=a; scl=b; hz=c; return init_ok; }
  void setTimeOut(int value) { timeout=value; }
  void beginTransmission(int address) { device=address; data_count=0; }
  void write(uint8_t) { ++data_count; }
  uint8_t endTransmission(bool = true) {
    if (data_count) return 2;  // No recognized QMI; only address polling follows.
    probes.push_back(device);
    if (device == 0x40) return 5;  // Simulated bus timeout.
    return present && (device == 0x15 || device == 0x6B) ? 0 : 2;
  }
  size_t requestFrom(uint8_t, size_t) { return 0; }
  int read() { return 0; }
};
inline WireStub Wire;
"""
    )
    (tmp_path / "ArduinoJson.h").write_text(
        """
#pragma once
#include <map>
#include <memory>
#include <string>
#include <type_traits>
#include <vector>
struct Node {
  std::map<std::string, std::shared_ptr<Node>> fields;
  std::vector<std::string> values;
  long long number = 0;
  std::string text;
};
struct JsonObject {
  std::shared_ptr<Node> node = std::make_shared<Node>();
  JsonObject operator[](const char* key) {
    auto& field = node->fields[key];
    if (!field) field = std::make_shared<Node>();
    return JsonObject{field};
  }
  template<class T> T to() { return T{node}; }
  template<class T> void operator=(T value) {
    if constexpr (std::is_arithmetic_v<T>) node->number = value;
    else node->text = value;
  }
};
struct JsonArray {
  std::shared_ptr<Node> node;
  void add(const char* value) { node->values.emplace_back(value); }
};
"""
    )
    source = tmp_path / "scan.cpp"
    source.write_text(
        """
#include <cassert>
#include "Wire.h"
#include "orientation.cpp"
int main() {
  vera_orientation::begin();
  assert(Wire.sda == 42 && Wire.scl == 41 && Wire.hz == 100000);
  for (int i=0; i<112; ++i) { now += 20; vera_orientation::update(); }
  JsonObject status;
  vera_orientation::status(status);
  auto bus = status["i2c"];
  if (!INIT_OK) {
    assert(Wire.probes.empty());
    assert(status["error"].node->text == "i2c_init_failed");
    assert(!bus["ready"].node->number);
    return 0;
  }
  assert(Wire.timeout == 20);
  assert(Wire.probes.size() == 112);
  for (int i=0; i<112; ++i) assert(Wire.probes[i] == i + 8);
  assert(bus["completed_scans"].node->number == 1);
  assert(!bus["scanning"].node->number);
  assert(bus["errors"].node->number == 1 && bus["last_error_code"].node->number == 5);
  assert((bus["addresses"].node->values == std::vector<std::string>{"0x15","0x6B"}));
  assert(status["error"].node->text == "imu_not_detected");
  now += 29999;
  vera_orientation::update();
  assert(Wire.probes.size() == 112);
  ++now;
  Wire.present = false;
  vera_orientation::update();
  assert(Wire.probes.size() == 113);
  for (int i=0; i<111; ++i) { now += 20; vera_orientation::update(); }
  JsonObject second;
  vera_orientation::status(second);
  assert(second["i2c"]["completed_scans"].node->number == 2);
  assert(second["i2c"]["addresses"].node->values.empty());
}
"""
    )
    exe = tmp_path / "scan"
    subprocess.run(
        [compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
         "-DVERA_DISPLAY_S3_TOUCH=1", f"-DINIT_OK={int(init_ok)}",
         "-I", str(tmp_path), "-I", str(root / "firmware/vera_display"),
         str(source), "-o", str(exe)],
        check=True, capture_output=True, text=True,
    )
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
