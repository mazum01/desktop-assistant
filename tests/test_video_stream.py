"""Compile and run the firmware's bounded MJPEG parser and scale/path guards."""

from pathlib import Path
import shutil
import subprocess

import pytest


def test_video_stream_bounds_and_scaling(tmp_path):
    compiler = shutil.which("g++")
    if not compiler:
        pytest.skip("g++ required for firmware helper tests")
    root = Path(__file__).resolve().parents[1]
    source = tmp_path / "stream.cpp"
    source.write_text(
        r"""
#include <cassert>
#include <string>
#include "video_stream.h"
#include "orientation_math.h"
using namespace vera_video_stream;
int main() {
  assert(valid_path("/videos/a.mjpg", 96));
  assert(valid_path("/videos/a.mjpeg", 96));
  for (auto path : {"/videos/.mjpg", "/videos/../a.mjpg", "/videos/a.mp4",
                    "/videos/x/a.mjpg", "/other/a.mjpg", "/videos/a\\b.mjpg"})
    assert(!valid_path(path, 96));
  assert(!valid_path(("/videos/" + std::string(100, 'x') + ".mjpg").c_str(), 96));
  assert(scale_for(320, 152, 320, 152, 2560) == 1);
  assert(scale_for(640, 300, 320, 152, 2560) == 2);
  assert(scale_for(1280, 608, 320, 152, 2560) == 4);
  assert(scale_for(1920, 1080, 320, 152, 2560) == 8);
  assert(scale_for(2560, 1440, 320, 152, 2560) == 0);
  assert(scale_for(0, 12, 320, 152, 2560) == 0);
  assert(scale_for(3000, 12, 320, 152, 2560) == 0);
  uint8_t buffer[6];
  FrameCollector frame;
  assert(frame.feed(1, buffer, sizeof(buffer)) == FrameCollector::kMore);
  assert(frame.length == 0);
  for (auto byte : {0xFF, 0xD8, 1, 2, 0xFF})
    assert(frame.feed(byte, buffer, sizeof(buffer)) == FrameCollector::kMore);
  assert(frame.feed(0xD9, buffer, sizeof(buffer)) == FrameCollector::kComplete);
  assert(frame.length == 6 && buffer[0] == 0xFF && buffer[5] == 0xD9);
  FrameCollector oversized;
  for (auto byte : {0xFF, 0xD8, 1, 2, 3, 4})
    assert(oversized.feed(byte, buffer, sizeof(buffer)) == FrameCollector::kMore);
  assert(oversized.feed(5, buffer, sizeof(buffer)) == FrameCollector::kOversized);
  FrameCollector truncated;
  for (auto byte : {0xFF, 0xD8, 1}) truncated.feed(byte, buffer, sizeof(buffer));
  assert(truncated.length == 3);
  vera_orientation::Angles angles;
  float level[] = {0, 0, 1, 0, 0, 10};
  angles.update(level, 0);
  assert(angles.roll == 0 && angles.pitch == 0 && angles.yaw == 0);
  for (int i = 0; i < 50; ++i) angles.update(level, 0.02f);
  assert(fabsf(angles.yaw - 10) < 0.01f);
  float tilted[] = {0, 0.70710678f, 0.70710678f, 0, 0, 0};
  angles.update(tilted, 1);  // A stale stream reinitializes from gravity.
  assert(fabsf(angles.roll - 45) < 0.01f && angles.yaw == 0);
  float pitched[] = {-0.5f, 0, 0.8660254f, 0, 0, 0};
  vera_orientation::Angles pitch;
  pitch.update(pitched, 0);
  assert(fabsf(pitch.pitch - 30) < 0.01f);
}
"""
    )
    exe = tmp_path / "stream"
    subprocess.run([compiler, "-std=c++17", "-Wall", "-Wextra", "-Werror",
                    "-I", str(root / "firmware/vera_display"), str(source), "-o", str(exe)],
                   check=True, capture_output=True, text=True)
    subprocess.run([str(exe)], check=True, capture_output=True, text=True)
