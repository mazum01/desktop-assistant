# Desktop Assistant — Requirements

> Printable requirements document.

## 1. Hardware

| Subsystem | Component | Notes |
|---|---|---|
| Compute | Raspberry Pi 5 (8 GB recommended) | Main SBC |
| AI Accel | Hailo-8 M.2 | via PCIe HAT |
| Servo Driver | SparkFun Pi Servo pHAT | I²C, PCA9685-based |
| Pan Servo | DS3218 | 270° mechanical range |
| Cooling | Noctua NF-A6x25 PWM | 5 V PWM fan |
| Temp Sensor | SparkFun Qwiic TMP117 | I²C, ±0.1 °C |
| Cameras | Stereo pair (2× CSI or USB) | synchronized capture |
| Microphones | Dual mic array | for DOA + noise reject |
| Audio Out | Sabrent USB Audio Adapter | → stereo speakers |

## 2. Functional Requirements

### 2.1 Motion (Pan)
- FR-M1: Logical pan range 1°–360°.
- FR-M2: Servo mechanical range is 270°; commands map to mechanical positions
  via calibration table.
- FR-M3: When commanded across the wrap (e.g., 350° → 10°), the controller
  **must traverse the long way** through the mechanical range (i.e., back
  through 340°, 300°, … to 10°) rather than crossing the dead zone.
- FR-M4: Configurable max angular velocity and acceleration.
- FR-M5: Soft limits with emergency stop API.

### 2.2 Thermal
- FR-T1: Sample TMP117 at ≥ 1 Hz.
- FR-T2: Drive NF-A6x25 PWM fan via closed-loop control on temperature.
- FR-T3: Define safe / warn / critical thresholds; emit events.
- FR-T4: Fail-safe to 100 % duty on sensor loss.

### 2.3 Audio
- FR-A1: Capture from dual mics at 16 kHz / 16-bit minimum.
- FR-A2: Output via Sabrent USB device, stereo.
- FR-A3: Wake-word detection always-on.
- FR-A4: VAD-gated streaming STT.

### 2.4 Vision
- FR-V1: Stereo capture, time-synchronized.
- FR-V2: Hailo-8 inference for person/face detection.
- FR-V3: Provide target azimuth to motion service for gaze tracking.

### 2.5 Assistant
- FR-AS1: Intent recognition + dialog state.
- FR-AS2: TTS playback through audio service.
- FR-AS3: Pluggable skill modules.

## 3. Non-Functional Requirements

- NFR-1: Cold boot to ready ≤ 60 s.
- NFR-2: Wake-to-response latency ≤ 1.5 s typical.
- NFR-3: 24 h soak without thermal or memory regressions.
- NFR-4: All services restart on failure (systemd `Restart=always`).
- NFR-5: Configuration via files in `/etc/desktop-assistant/`.
- NFR-6: Logging via `journald`; structured JSON where practical.

## 4. Software Stack

- OS: Raspberry Pi OS (64-bit, Bookworm)
- Language: Python 3.11+ (with C extensions where needed)
- AI runtime: HailoRT
- Audio: ALSA / PipeWire
- IPC: DBus or ZeroMQ (TBD)
- Process mgmt: systemd

## 5. Out of Scope (initial release)

- Tilt axis (pan only)
- Battery / portable operation
- Cloud LLM offload (future)
