"""Host-side checks for the two display hardware profiles and panel boot data."""

from pathlib import Path
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("legacy", [False, True])
def test_board_contract_and_panel_boot(tmp_path, legacy):
    compiler = shutil.which("g++")
    if compiler is None:
        pytest.skip("g++ is required for the firmware header test")
    root = Path(__file__).resolve().parents[1]
    (tmp_path / "Arduino_GFX_Library.h").write_text(
        """
#pragma once
#include <cstdint>
#include <vector>
#include <utility>
inline std::vector<int> delays;
inline void delay(int ms) { delays.push_back(ms); }
struct Arduino_DataBus {
  std::vector<std::pair<int, std::vector<int>>> registers;
  int depth = 0;
  void beginWrite() { ++depth; }
  void endWrite() { --depth; }
  void writeCommand(uint8_t command) { registers.push_back({command, {}}); }
  void write(uint8_t value) { registers.back().second.push_back(value); }
};
"""
    )
    source = tmp_path / "contract.cpp"
    source.write_text(
        """
#include <cassert>
#include "display_board.h"
int main() {
  using namespace vera_board;
  static_assert(CS == 14 && DC == 15 && ROTATION == 1);
  static_assert(BACKLIGHT_PERCENT == 50);
#if defined(VERA_DISPLAY_LEGACY_LCD)
  static_assert(MOSI == 6 && SCLK == 7 && RST == 21 && BACKLIGHT == 22);
#else
  static_assert(MOSI == 2 && SCLK == 1 && RST == 22 && BACKLIGHT == 23);
  Arduino_DataBus bus;
  initialize_touch_panel(&bus);
  assert(bus.depth == 0);
  assert((delays == std::vector<int>{120, 10}));
  assert(bus.registers.size() == 33);
  assert(bus.registers.front().first == 0x11);
  assert(bus.registers.back().first == 0x29);
  assert(bus.registers[1].first == 0xDF);
  assert((bus.registers[1].second == std::vector<int>{0x98, 0x53}));
  bool columns = false, rows = false, rgb565 = false;
  for (const auto &reg : bus.registers) {
    if (reg.first == 0x2A)
      columns = reg.second == std::vector<int>{0, 34, 0, 205};
    if (reg.first == 0x2B)
      rows = reg.second == std::vector<int>{0, 0, 1, 63};
    if (reg.first == 0x3A)
      rgb565 = reg.second == std::vector<int>{5};
  }
  assert(columns && rows && rgb565);
  assert(bus.registers[bus.registers.size() - 2].first == 0x21);
#endif
}
"""
    )
    executable = tmp_path / "contract"
    command = [
        compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
        "-I", str(tmp_path), "-I", str(root / "firmware" / "vera_display"),
        str(source), "-o", str(executable),
    ]
    if legacy:
        command.append("-DVERA_DISPLAY_LEGACY_LCD=1")
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)
