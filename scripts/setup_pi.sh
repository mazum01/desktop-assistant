#!/usr/bin/env bash
# setup_pi.sh — Install all Desktop Assistant dependencies on Raspberry Pi OS Bookworm
# Run once after cloning: bash scripts/setup_pi.sh

set -euo pipefail

echo "╔══════════════════════════════════════════════════╗"
echo "║   Desktop Assistant — Pi dependency installer   ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── System packages ──────────────────────────────────────────────────
echo "[1/4] Installing system packages..."
sudo apt-get update -qq
sudo apt-get install -y \
    python3-pip \
    python3-venv \
    python3-smbus \
    i2c-tools \
    python3-lgpio \
    libasound2-dev \
    portaudio19-dev

# ── Enable I2C if not already enabled ────────────────────────────────
echo ""
echo "[2/4] Checking I²C status..."
if ! grep -q "^dtparam=i2c_arm=on" /boot/firmware/config.txt 2>/dev/null &&
   ! grep -q "^dtparam=i2c_arm=on" /boot/config.txt 2>/dev/null; then
    echo "  Enabling I²C in /boot/firmware/config.txt..."
    echo "dtparam=i2c_arm=on" | sudo tee -a /boot/firmware/config.txt > /dev/null
    echo "  ⚠ Reboot required for I²C to activate."
else
    echo "  ✓ I²C already enabled"
fi

# ── Python venv + pip packages ───────────────────────────────────────
echo ""
echo "[3/4] Creating Python venv at ~/.venv-assistant..."
python3 -m venv ~/.venv-assistant
# shellcheck source=/dev/null
source ~/.venv-assistant/bin/activate

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pip install --quiet --upgrade pip
pip install --quiet -r "$REPO_DIR/requirements.txt"
pip install --quiet adafruit-circuitpython-servokit

echo "  ✓ Python packages installed"

# ── Verify I2C devices ───────────────────────────────────────────────
echo ""
echo "[4/4] I²C device scan (bus 1):"
i2cdetect -y 1 || echo "  (i2cdetect failed — may need reboot if I²C was just enabled)"

echo ""
echo "══════════════════════════════════════════════════"
echo "Setup complete."
echo ""
echo "Activate the venv before running any scripts:"
echo "  source ~/.venv-assistant/bin/activate"
echo ""
echo "Then test each device:"
echo "  python scripts/test_tmp117.py"
echo "  python scripts/test_servo.py"
echo "  python scripts/test_fan.py"
echo "══════════════════════════════════════════════════"
