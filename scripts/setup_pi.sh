#!/usr/bin/env bash
# setup_pi.sh — Install all Desktop Assistant dependencies on Raspberry Pi OS Bookworm
#
# Uses SYSTEM Python (no venv). All hardware libs (picamera2, libcamera,
# lgpio) are apt-only on Pi 5; trying to push them through a venv adds
# friction with no benefit on a dedicated appliance.
#
# Run once after cloning:  bash scripts/setup_pi.sh

set -euo pipefail

echo "╔══════════════════════════════════════════════════╗"
echo "║   Desktop Assistant — Pi dependency installer   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. System packages (apt) ─────────────────────────────────────────
echo "[1/4] Installing system packages (apt)..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-smbus \
    python3-numpy \
    python3-lgpio \
    python3-gpiozero \
    python3-picamera2 \
    python3-sounddevice \
    python3-soundfile \
    python3-pytest \
    i2c-tools \
    libasound2-dev \
    portaudio19-dev

# ── 2. Pip packages (system, --break-system-packages on PEP 668) ─────
echo ""
echo "[2/4] Installing extra pip packages (system Python)..."
# These have no apt equivalent on Bookworm. Use --break-system-packages
# because Bookworm enforces PEP 668 by default.
sudo pip3 install --quiet --break-system-packages \
    smbus2 \
    Adafruit-Blinka \
    adafruit-circuitpython-servokit

# ── 3. Enable I²C if needed ──────────────────────────────────────────
echo ""
echo "[3/4] Checking I²C status..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null &&
   ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null; then
    echo "  Enabling I²C in /boot/firmware/config.txt..."
    echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt > /dev/null
    echo "  ⚠ Reboot required for I²C to activate."
else
    echo "  ✓ I²C already enabled"
fi

# ── 4. Verify ────────────────────────────────────────────────────────
echo ""
echo "[4/4] Verifying installation..."
python3 -c "import picamera2; print('  ✓ picamera2:', picamera2.__version__ if hasattr(picamera2, '__version__') else 'OK')" || echo "  ✗ picamera2 missing"
python3 -c "import smbus2; print('  ✓ smbus2 OK')" || echo "  ✗ smbus2 missing"
python3 -c "import lgpio; print('  ✓ lgpio OK')" || echo "  ✗ lgpio missing"
python3 -c "from adafruit_servokit import ServoKit; print('  ✓ adafruit-servokit OK')" || echo "  ✗ adafruit-servokit missing"
python3 -c "import numpy; print('  ✓ numpy', numpy.__version__)" || echo "  ✗ numpy missing"

echo ""
echo "I²C device scan (bus 1):"
i2cdetect -y 1 || echo "  (i2cdetect failed — may need reboot if I²C was just enabled)"

echo ""
echo "══════════════════════════════════════════════════"
echo "Setup complete. No venv — runs directly with system python3."
echo ""
echo "Test each device:"
echo "  python3 scripts/test_tmp117.py"
echo "  python3 scripts/test_servo.py"
echo "  python3 scripts/test_fan.py"
echo "  python3 scripts/test_camera.py"
echo ""
echo "If you have an old venv from earlier setup, you can remove it:"
echo "  rm -rf ~/.venv-assistant"
echo "══════════════════════════════════════════════════"
