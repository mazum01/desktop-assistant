"""Host-side checks for display hardware profiles and panel boot data."""

from pathlib import Path
import os
import shutil
import subprocess

import pytest


@pytest.mark.parametrize("profile", ["touch", "legacy", "s3-touch"])
def test_board_contract_and_panel_boot(tmp_path, profile):
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
#if defined(VERA_DISPLAY_S3_TOUCH)
  static_assert(ROTATION == 3 && PANEL_MADCTL == 0xA0);
#else
  static_assert(ROTATION == 1);
#endif
  static_assert(BACKLIGHT_PERCENT == 50);
#if defined(VERA_DISPLAY_LEGACY_LCD)
  static_assert(CS == 14 && DC == 15);
  static_assert(MOSI == 6 && SCLK == 7 && RST == 21 && BACKLIGHT == 22);
#else
#if defined(VERA_DISPLAY_S3_TOUCH)
  static_assert(CS == 21 && DC == 45);
  static_assert(MOSI == 39 && SCLK == 38 && RST == 40 && BACKLIGHT == 46);
#else
  static_assert(CS == 14 && DC == 15);
  static_assert(MOSI == 2 && SCLK == 1 && RST == 22 && BACKLIGHT == 23);
#endif
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
    if profile == "legacy":
        command.append("-DVERA_DISPLAY_LEGACY_LCD=1")
    elif profile == "s3-touch":
        command.append("-DVERA_DISPLAY_S3_TOUCH=1")
    subprocess.run(command, check=True, capture_output=True, text=True)
    subprocess.run([str(executable)], check=True, capture_output=True, text=True)


@pytest.mark.parametrize("profile,chip,flag", [
    ("s3-touch", "esp32s3", "VERA_DISPLAY_S3_TOUCH"),
    ("touch", "esp32c6", ""),
    ("legacy", "esp32c6", "VERA_DISPLAY_LEGACY_LCD"),
])
def test_upload_build_profiles_agree(profile, chip, flag):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "VERA_DISPLAY_BOARD": profile}
    env.pop("VERA_ESP32_FQBN", None)
    result = subprocess.run(
        ["bash", "-c", 'source firmware/display_board_config.sh; printf "%s\\n" "$FQBN" "${BOARD_FLAGS[@]}"'],
        cwd=root, env=env, check=True, capture_output=True, text=True,
    )
    expected = f"esp32:esp32:{chip}" + (":PSRAM=opi" if profile == "s3-touch" else "")
    assert result.stdout.splitlines()[0] == expected
    if flag:
        assert flag in result.stdout
    env["VERA_ESP32_FQBN"] = "esp32:esp32:esp32s3" if chip == "esp32c6" else "esp32:esp32:esp32c6"
    rejected = subprocess.run(
        ["bash", "-c", "source firmware/display_board_config.sh"],
        cwd=root, env=env, capture_output=True, text=True,
    )
    assert rejected.returncode == 2
    assert "requires" in rejected.stderr


def test_canvas_does_not_reset_custom_panel_initialization():
    root = Path(__file__).resolve().parents[1]
    renderer = (root / "firmware/vera_display/mouth_renderer.cpp").read_text()
    assert "g_gfx->begin(GFX_SKIP_OUTPUT_BEGIN)" in renderer
    assert "g_gfx->begin()" not in renderer


def test_default_mouth_is_a_slight_smile_at_85_percent_screen_width():
    root = Path(__file__).resolve().parents[1]
    renderer = (root / "firmware/vera_display/mouth_renderer.cpp").read_text()
    assert "DEFAULT_MOUTH_WIDTH_FRAC = 0.85f / MOUTH_WIDTH_SCALE" in renderer
    assert "kNeutral{Shape::kSmile, DEFAULT_MOUTH_WIDTH_FRAC, 0.09f, 0.0f, 0.25f" in renderer
    assert "draw_smile(x, y, w, h, geo.curve_frac, geo.color)" in renderer
    assert "draw_curve(x, y, w, h, geo.curve_frac, /*bulge_down=*/true" not in renderer


def test_no_orientation_footer_on_screen():
    """The S3 board has no IMU, so nothing reserves or draws a footer strip."""
    sketch = Path(__file__).resolve().parents[1] / "firmware" / "vera_display"
    sources = {p.name: p.read_text() for p in sketch.iterdir() if p.suffix in (".cpp", ".h", ".ino")}
    assert not any("FOOTER_" in text for text in sources.values())
    assert "vera_orientation::draw" not in "".join(sources.values())
    assert "void draw()" not in sources["orientation.h"]
