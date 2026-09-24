#!/usr/bin/env bash
# Bootstrap a clean Raspberry Pi OS Bookworm host for a new VERA instance.
#
# Copy this file to the target Pi and run it after first boot:
#   bash bootstrap_vera3.sh --user starter --ref <commit-or-branch>
#
# It deliberately does not copy secrets, OpenClaw state, face data, calibration,
# or machine-specific configuration from another VERA.

set -euo pipefail

DEFAULT_REPO_URL="https://github.com/mazum01/desktop-assistant.git"
REPO_URL="$DEFAULT_REPO_URL"
REF="main"
TARGET_USER="${SUDO_USER:-${USER}}"
TARGET_DIR=""
WITH_HAILO=false
INSTALL_UNITS=true

usage() {
    cat <<'EOF'
Usage: bash bootstrap_vera3.sh [options]

Install a fresh VERA software baseline on Raspberry Pi OS Bookworm (64-bit).

Options:
  --user USER       Account that will run VERA (default: current non-root user)
  --dir PATH        Clone location (default: /home/USER/Code/Desktop Assistant)
  --ref REF         Git branch, tag, or commit to install (default: main)
  --repo-url URL    Repository URL (default: official GitHub repository)
  --with-hailo      Install hailo-all after the base setup
  --no-units        Do not install systemd/udev unit files
  -h, --help        Show this help

The script installs dependencies and unit files but does NOT enable VERA
services. Follow docs/VERA3_SETUP.md to verify hardware and configure the
new machine before enabling its services.
EOF
}

die() {
    echo "ERROR: $*" >&2
    exit 1
}

while (($#)); do
    case "$1" in
        --user) TARGET_USER="${2:-}"; shift 2 ;;
        --dir) TARGET_DIR="${2:-}"; shift 2 ;;
        --ref) REF="${2:-}"; shift 2 ;;
        --repo-url) REPO_URL="${2:-}"; shift 2 ;;
        --with-hailo) WITH_HAILO=true; shift ;;
        --no-units) INSTALL_UNITS=false; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "Unknown argument: $1" ;;
    esac
done

[[ -n "$TARGET_USER" && "$TARGET_USER" != "root" ]] || die "Use --user with a non-root account."
id "$TARGET_USER" >/dev/null 2>&1 || die "User '$TARGET_USER' does not exist."
[[ -n "$REF" && -n "$REPO_URL" ]] || die "--ref and --repo-url cannot be empty."

TARGET_HOME="$(getent passwd "$TARGET_USER" | cut -d: -f6)"
[[ -n "$TARGET_HOME" && -d "$TARGET_HOME" ]] || die "Could not determine a home directory for $TARGET_USER."
TARGET_DIR="${TARGET_DIR:-$TARGET_HOME/Code/Desktop Assistant}"
[[ "$TARGET_DIR" == "$TARGET_HOME/"* ]] || die "--dir must be inside $TARGET_HOME."

if [[ "$(uname -m)" != "aarch64" ]]; then
    die "VERA requires 64-bit Raspberry Pi OS (expected aarch64; got $(uname -m))."
fi
if ! grep -qi 'bookworm' /etc/os-release; then
    die "VERA's supported baseline is Raspberry Pi OS Bookworm."
fi

sudo -v
echo "==> Updating Raspberry Pi OS packages"
sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y

echo "==> Preparing VERA account permissions"
for group in i2c gpio audio video plugdev; do
    getent group "$group" >/dev/null || sudo groupadd --system "$group"
    sudo usermod -aG "$group" "$TARGET_USER"
done

echo "==> Obtaining VERA source at $REF"
if [[ -e "$TARGET_DIR" && ! -d "$TARGET_DIR/.git" ]]; then
    die "Target path exists but is not a Git clone: $TARGET_DIR"
fi
if [[ ! -d "$TARGET_DIR/.git" ]]; then
    sudo -u "$TARGET_USER" mkdir -p "$(dirname "$TARGET_DIR")"
    sudo -u "$TARGET_USER" git clone "$REPO_URL" "$TARGET_DIR"
fi
sudo -u "$TARGET_USER" git -C "$TARGET_DIR" fetch --tags --prune origin
sudo -u "$TARGET_USER" git -C "$TARGET_DIR" checkout --detach "$REF"
sudo -u "$TARGET_USER" git -C "$TARGET_DIR" submodule update --init --recursive

echo "==> Installing VERA runtime prerequisites"
bash "$TARGET_DIR/scripts/setup_pi.sh"

if "$WITH_HAILO"; then
    echo "==> Installing Hailo runtime"
    sudo apt-get install -y hailo-all
else
    echo "==> Hailo runtime skipped (run this script with --with-hailo after fitting the HAT)."
fi

echo "==> Preparing secure configuration locations"
sudo install -d -m 0700 /etc/desktop-assistant
sudo install -m 0600 -o root -g root /dev/null /etc/desktop-assistant/secrets.env
sudo install -D -m 0644 \
    "$TARGET_DIR/services/udev/70-respeaker-flex-xvf.rules" \
    /etc/udev/rules.d/70-respeaker-flex-xvf.rules
sudo udevadm control --reload-rules

if "$INSTALL_UNITS"; then
    echo "==> Installing VERA systemd unit files (not enabling them)"
    for unit in "$TARGET_DIR"/services/systemd/desktop-assistant-*.service; do
        destination="/etc/systemd/system/$(basename "$unit")"
        sed \
            -e "s|User=starter|User=$TARGET_USER|g" \
            -e "s|Group=starter|Group=$TARGET_USER|g" \
            -e "s|/home/starter/Code/Desktop Assistant|$TARGET_DIR|g" \
            -e "s|/home/starter|$TARGET_HOME|g" \
            "$unit" | sudo tee "$destination" >/dev/null
        sudo chmod 0644 "$destination"
    done
    sudo systemctl daemon-reload
fi

echo
echo "VERA baseline installed successfully."
echo "Installed revision: $(git -C "$TARGET_DIR" rev-parse --short HEAD)"
echo "Repository: $TARGET_DIR"
echo
echo "REQUIRED NEXT STEPS:"
echo "  1. Reboot now so package/kernel, I2C/PWM, and group changes apply."
echo "  2. Follow $TARGET_DIR/docs/VERA3_SETUP.md from 'Post-bootstrap hardware bring-up'."
echo "  3. Configure unique VERA3 credentials, BLE address, calibration, and network values."
echo "  4. Do not enable desktop-assistant services until the hardware checks pass."
