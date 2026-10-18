#pragma once

#include <math.h>

namespace vera_orientation {
struct Angles {
  float roll = 0, pitch = 0, yaw = 0;
  bool initialized = false;

  void update(const float v[6], float dt) {
    constexpr float TO_DEGREES = 57.295779513f;
    constexpr float TO_RADIANS = 0.01745329252f;
    float ar = atan2f(v[1], v[2]) * TO_DEGREES;
    float ap = atan2f(-v[0], hypotf(v[1], v[2])) * TO_DEGREES;
    if (!initialized || dt > 0.5f) {
      roll = ar;
      pitch = ap;
      yaw = 0;
      initialized = true;
      return;
    }
    float norm = sqrtf(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]);
    float weight = (norm > 0.8f && norm < 1.2f) ? 1 - expf(-dt / 0.5f) : 0;
    float r = roll * TO_RADIANS, p = pitch * TO_RADIANS;
    // Convert body gyro rates to Euler rates; yaw remains unreferenced/drifting.
    if (fabsf(cosf(p)) > 0.15f) {
      float rd = v[3] + sinf(r) * tanf(p) * v[4] + cosf(r) * tanf(p) * v[5];
      float pd = cosf(r) * v[4] - sinf(r) * v[5];
      float yd = (sinf(r) * v[4] + cosf(r) * v[5]) / cosf(p);
      roll += rd * dt;
      pitch += pd * dt;
      yaw = remainderf(yaw + yd * dt, 360.0f);
    }
    roll += weight * remainderf(ar - roll, 360.0f);
    pitch += weight * (ap - pitch);
    roll = remainderf(roll, 360.0f);
  }
};
}  // namespace vera_orientation
