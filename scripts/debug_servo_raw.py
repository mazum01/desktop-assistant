#!/usr/bin/env python3
"""
Minimal direct PCA9685 servo test — no abstraction layers.

Directly writes I2C registers and sends specific pulses to channel 15.
Run this on the Pi to isolate exactly where the problem is.

Usage:
    python scripts/debug_servo_raw.py
"""

import sys
import time

try:
    import smbus2
except ImportError:
    print("ERROR: smbus2 not installed. Run: pip install smbus2")
    sys.exit(1)

I2C_BUS     = 1
PCA_ADDR    = 0x40
CHANNEL     = 15
FREQ_HZ     = 50.0

# PCA9685 registers
MODE1       = 0x00
PRESCALE    = 0xFE
# Channel 15 base register: 0x06 + 4*15 = 0x42
CH15_ON_L   = 0x06 + 4 * CHANNEL   # = 0x42

def write(bus, reg, val):
    bus.write_byte_data(PCA_ADDR, reg, val)
    print(f"  WRITE reg=0x{reg:02X} val=0x{val:02X} ({val})")

def read(bus, reg):
    val = bus.read_byte_data(PCA_ADDR, reg)
    print(f"  READ  reg=0x{reg:02X} val=0x{val:02X} ({val})")
    return val

def set_pulse_us(bus, pulse_us):
    """Set pulse width in microseconds on channel 15."""
    period_us = 1_000_000.0 / FREQ_HZ
    off = round(pulse_us / period_us * 4096)
    off = max(0, min(4095, off))
    on  = 0
    data = [on & 0xFF, (on >> 8) & 0x0F, off & 0xFF, (off >> 8) & 0x0F]
    bus.write_i2c_block_data(PCA_ADDR, CH15_ON_L, data)
    print(f"  PULSE {pulse_us}µs → off_count={off}  bytes={[hex(b) for b in data]}")

print("=" * 55)
print(f"PCA9685 raw servo test — bus={I2C_BUS} addr=0x{PCA_ADDR:02X} ch={CHANNEL}")
print("=" * 55)

# ── Step 1: open bus ─────────────────────────────────────────────────
print("\n[1] Opening I²C bus...")
try:
    bus = smbus2.SMBus(I2C_BUS)
    print("  ✓ Bus opened")
except Exception as e:
    print(f"  FAIL: {e}")
    sys.exit(1)

# ── Step 2: read MODE1 ───────────────────────────────────────────────
print("\n[2] Reading MODE1 register (should be 0x11 after power-on)...")
try:
    m1 = read(bus, MODE1)
except Exception as e:
    print(f"  FAIL — cannot read PCA9685: {e}")
    sys.exit(1)

# ── Step 3: set prescaler for 50 Hz ─────────────────────────────────
print("\n[3] Setting PWM frequency to 50 Hz...")
prescale = round(25_000_000 / (4096 * FREQ_HZ)) - 1
print(f"  prescale value = {prescale}")
try:
    write(bus, MODE1, (m1 & 0x7F) | 0x10)   # sleep
    write(bus, PRESCALE, prescale)
    write(bus, MODE1, m1)
    time.sleep(0.001)
    write(bus, MODE1, m1 | 0x80)             # restart
    time.sleep(0.001)
    print("  ✓ Frequency set")
except Exception as e:
    print(f"  FAIL: {e}")
    sys.exit(1)

# ── Step 4: send pulses ──────────────────────────────────────────────
pulses = [
    (1500, "centre (135° mechanical)"),
    (500,  "minimum (0° mechanical)"),
    (2500, "maximum (270° mechanical)"),
    (1500, "back to centre"),
]

print(f"\n[4] Sending pulses to channel {CHANNEL}...")
print("    Watch the servo — it should move between each pause.\n")

for pulse_us, label in pulses:
    print(f"  → {pulse_us}µs  [{label}]")
    try:
        set_pulse_us(bus, pulse_us)
    except Exception as e:
        print(f"    FAIL writing pulse: {e}")
        bus.close()
        sys.exit(1)
    time.sleep(2)

bus.close()
print("\n[5] Done.")
print("    If the servo did NOT move, check:")
print("    a) Servo power supply — channel rail needs external 5V/6V,")
print("       NOT just the Pi's 5V pin (insufficient current).")
print("    b) Signal wire connected to channel 15 signal pin (not GND/VCC).")
print("    c) Servo wiring: signal=orange/yellow, +V=red, GND=brown/black.")
