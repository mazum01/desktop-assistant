# DS3218 Servo — Hardware Notes

## Overview
- Model: DS3218 digital servo
- Rotation: **270° mechanical range**
- Torque: 21 kg/cm (6V)
- Voltage: 4.8 – 7.2 V (recommend 5 V regulated)
- Pulse width: 500 µs (0°) → 2500 µs (270°)
- Frequency: 50 Hz standard

## Driver Board
SparkFun Pi Servo pHAT — PCA9685 I²C PWM controller  
Default I²C address: **0x40**  
I²C bus: **1** (pins 3/5 on the Pi header)  
Supported channels: **0–15**  
**Pan servo connected to channel 15**

## Wiring
```
Pi Header  →  Servo pHAT
3V3        →  (not used for logic — pHAT uses I2C)
Pin 3 SDA  →  SDA
Pin 5 SCL  →  SCL
GND        →  GND

Servo pHAT Channel 0  →  DS3218 signal wire (usually orange/yellow)
External 5 V supply   →  Servo pHAT VCC rail
GND (shared)          →  Servo GND (black/brown)
```

## Logical ↔ Mechanical Angle Mapping
| Logical (°) | Mechanical (°) | Pulse (µs) |
|-------------|----------------|------------|
| 1           | 0              | 500        |
| 90          | 67.2           | 996        |
| 180         | 134.5          | 1493       |
| 270         | 201.8          | 1990       |
| 360         | 269.9          | 2496       |

Formula: `mechanical = (logical - 1) * (270 / 359)`

## Wrap Traversal Rule
The logical range is 1°–360°.  
Between 360° and 1° there is a **dead zone** — the 90° of servo travel
that the mechanical range does not cover.  
**The controller must NEVER cross this dead zone.**  
When commanded from a higher logical angle to a lower one
(e.g., 350° → 10°), the servo must travel **backward** through decreasing
angles (350 → 340 → … → 10) instead of crossing the 360/1 boundary.

## Calibration
Pulse widths may need fine-tuning per unit.  
Edit `config/servo.yaml` to override min/max pulse widths.
