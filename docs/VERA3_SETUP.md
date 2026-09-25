# VERA3: Clean Raspberry Pi OS Installation

This is the end-to-end runbook for a new VERA. It begins with a fresh
Raspberry Pi 5 and ends with all VERA services running. It intentionally does
not clone operational state from another VERA.

## What you need

### Required baseline

- Raspberry Pi 5, **64-bit Raspberry Pi OS Bookworm or Trixie**, and a 27 W
  USB-C power supply.
- Active cooling and reliable storage; an NVMe SSD is strongly recommended for
  a production unit.
- Network access during installation.
- A user account named `starter` is recommended. The bootstrap script supports
  other accounts, but existing service files have historically used `starter`.

### VERA hardware

- SparkFun Pi Servo pHAT and DS3218 pan servo.
- SparkFun Qwiic TMP117 and Noctua NF-A6x25 PWM fan.
- At least one Camera Module 3 or compatible Pi camera.
- ReSpeaker Flex XVF3800 and a supported USB audio output device.
- Optional: a second camera, Hailo-8 AI HAT+, and Waveshare ESP32-C6 display.

Do not power the DS3218 from the Pi's 5 V rail. Use the servo pHAT's correctly
rated external servo supply and connect grounds as specified by the pHAT and
servo documentation.

## 1. Image and boot Raspberry Pi OS

Use Raspberry Pi Imager to write **Raspberry Pi OS Lite (64-bit)** on either
Bookworm or Trixie. Trixie is the recommended current release for a new image;
Bookworm remains supported for parity with existing VERA units.
In the Imager customization screen:

1. Set a unique hostname, such as `vera3`.
2. Create the `starter` account and a strong password.
3. Configure locale, keyboard, Wi-Fi, and SSH.
4. Boot the Pi and connect over SSH.

Verify the supported platform:

```bash
uname -m                 # expected: aarch64
python3 --version        # expected: Python 3.11 or newer
grep PRETTY_NAME /etc/os-release
vcgencmd get_throttled   # expected: throttling flags 0x0
```

## 2. Run the first-boot bootstrap

Copy `scripts/bootstrap_vera3.sh` to the new Pi using `scp`, a USB drive, or
another trusted transfer method. Run it as the newly-created account:

```bash
bash bootstrap_vera3.sh --user starter --ref 36ae2b0
```

`36ae2b0` is VERA v1.62.2. To use a newer reviewed revision, replace it with
that commit, branch, or release tag. Fit the Hailo-8 HAT+ first and add
`--with-hailo` only when you want the Hailo runtime installed:

```bash
bash bootstrap_vera3.sh --user starter --ref 36ae2b0 --with-hailo
```

The bootstrap checks out the selected revision in detached-HEAD mode to keep
the installed build reproducible. To update an already-bootstrapped Pi, fetch
then explicitly check out the desired revision; do not use bare `git pull`:

```bash
cd ~/Code/"Desktop Assistant"
git fetch --tags origin
git checkout --detach 32ebb79
```

The bootstrap:

- validates 64-bit Bookworm or Trixie;
- fully updates the operating system;
- clones the requested VERA revision;
- installs Pi, audio, camera, IPC, web, BLE, and Python dependencies;
- enables I2C and the GPIO13 hardware-PWM overlay;
- installs VERA's systemd and ReSpeaker udev files without enabling VERA;
- creates an empty root-owned `/etc/desktop-assistant/secrets.env`.

VERA runs directly on the system Python. Pi-owned hardware bindings are
installed through APT; VERA packages absent or too old in the Raspberry Pi OS
repositories are
installed globally under `/usr/local` through pip without removing
Debian-owned Python files. No virtual environment is created or required.

It does **not** copy secrets, OpenClaw state, face data, Telegram settings,
camera calibration, display identity, or configuration from another machine.

Reboot immediately after it completes:

```bash
sudo reboot
```

Log in again so the `starter` account's new `i2c`, `gpio`, `audio`, `video`,
and `plugdev` group memberships take effect:

```bash
id
```

## 3. Post-bootstrap hardware bring-up

Do this before enabling any VERA service.

### I2C, temperature, fan, and servo controller

```bash
cd ~/Code/"Desktop Assistant"
i2cdetect -y 1
python3 scripts/test_tmp117.py
python3 scripts/test_fan.py
```

The Pi Servo pHAT's PCA9685 must appear at `0x40`. The TMP117 address depends
on its address pin/wiring. If either is absent, stop and correct I2C wiring
before continuing.

The DS3218 pan servo belongs on **pHAT channel 15**. Before testing it, ensure
the assembly cannot hit a mechanical end stop. Run:

```bash
python3 scripts/test_servo.py
```

The test verifies VERA's required wrap-safe path for `350° → 10°`; it must
traverse backward through the usable mechanical range, never through the
servo's dead zone.

### Cameras

Connect the first camera to Pi 5 CAM/DISP 0 with the correct 22-pin FPC cable:

```bash
rpicam-hello --list-cameras || libcamera-hello --list-cameras
python3 scripts/test_camera.py
```

Add the second camera only after camera 1 passes. Set `camera2.enabled: true`
and the correct device index/orientation in `config/assistant.yaml`.

### Audio

Connect the ReSpeaker and USB audio adapter:

```bash
aplay -l
arecord -l
python3 scripts/test_speaker.py
python3 scripts/test_microphone.py
python3 scripts/test_tts.py
```

Confirm the device names match the `audio` configuration before enabling
voice commands.

### Optional Hailo-8

```bash
hailortcli fw-control identify
python3 scripts/test_hailo.py
```

Do not enable Hailo-backed features until both commands pass. VERA will fall
back to CPU inference if the accelerator is unavailable.

### Optional ESP32 display

Flash and verify the display using [the firmware instructions](../firmware/vera_display/README.md).
Set `display.ble_address` to **this display's** MAC address; do not reuse the
address from another VERA.

## 4. Create VERA3-specific configuration

Review `config/assistant.yaml` before starting VERA. At minimum, set:

- `display.ble_address` for the VERA3 display, or set `display.enabled: false`;
- camera indexes and `rotation_deg`;
- servo pulse/safe limits, inversion, and tracking settings after physical
  calibration;
- audio device names;
- `watchdog.wifi_connection_id` to VERA3's NetworkManager connection name;
- VERA3-specific quiet hours and notification preferences.

Leave optional integrations disabled until they have their own credentials and
have been tested.

Put only environment secrets in `/etc/desktop-assistant/secrets.env`, owned by
root and mode `600`. Never copy VERA1/VERA2's secret file. Typical entries
depend on the integrations you enable:

```bash
sudoedit /etc/desktop-assistant/secrets.env
sudo stat -c '%a %U:%G %n' /etc/desktop-assistant/secrets.env
# expected: 600 root:root /etc/desktop-assistant/secrets.env
```

Use a separate Telegram bot token and chat configuration for VERA3 unless you
intentionally want both units receiving the same bot's updates.

## 5. Build optional local components

Build the accelerated SCRFD decoder when using Hailo face detection:

```bash
cd ~/Code/"Desktop Assistant"
bash scripts/build_scrfd_decode.sh
```

Download the configured Piper voices if they are not in the repository:

```bash
bash scripts/download_voices.sh
```

Run the targeted tests before service activation:

```bash
pytest -q tests/test_hailo_probe.py tests/test_perception.py \
  tests/test_audio_factory.py tests/test_tts.py tests/test_watchdog.py
```

## 6. Start VERA services

Confirm every required hardware check passed, then enable the canonical
system-level services:

```bash
sudo systemctl enable --now \
  desktop-assistant-thermal.service \
  desktop-assistant-core.service \
  desktop-assistant-media.service \
  desktop-assistant-integrations.service \
  desktop-assistant-web.service \
  desktop-assistant-watchdog.service
```

Verify them:

```bash
systemctl --no-pager --full status \
  desktop-assistant-thermal \
  desktop-assistant-core \
  desktop-assistant-media \
  desktop-assistant-integrations \
  desktop-assistant-web \
  desktop-assistant-watchdog
curl --fail http://127.0.0.1:8080/health
```

The dashboard should then be available at `http://vera3.local:8080/` or the
Pi's LAN address. Do not expose it to the public internet until the TLS,
authentication, and rate-limiting backlog items in [TODO.md](TODO.md) are
complete.

## 7. Add OpenClaw only after VERA is stable

OpenClaw is a separate per-user installation. Do **not** copy
`~/.openclaw` from another VERA.

1. Install the supported Node.js/OpenClaw version for `starter`.
2. Install the OpenClaw Gateway with its own CLI-managed **user** service.
3. Authenticate it with the intended OpenAI/Codex account.
4. Copy only the VERA skill source directories from `.github/skills/` into
   `~/.openclaw/workspace/skills/`.
5. Configure a unique Telegram bot/channel, then verify:

   ```bash
   systemctl --user status openclaw-gateway.service
   openclaw gateway status --require-rpc
   ```

Only after the Gateway is healthy should you enable its watchdog monitoring in
`config/assistant.yaml`. During an OpenClaw upgrade, pause
`desktop-assistant-watchdog.service` until the new Gateway has completed its
first startup and health check, then restore the watchdog.

## 8. Final acceptance

Before treating VERA3 as operational, verify:

- it announces the installed version at boot;
- camera, audio, servo, TMP117, fan, and Hailo tests pass where fitted;
- the servo cannot exceed its calibrated physical range;
- a reboot brings every enabled service back;
- the dashboard health endpoint succeeds;
- a 24-hour soak test has no throttling, restart loop, camera failure, or
  thermal safety regression.

Keep the bootstrap script and the exact installed Git commit with the VERA3
hardware records so the installation can be reproduced.
