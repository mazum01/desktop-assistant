# VERA — Remaining Work

> Extracted from `PROJECT_PHASES.md`. Items already completed are omitted.
> Reference the phase docs for full context, exit criteria, and design notes.

---

## Phase 1 — Hardware Bring-Up

Smoke-test scripts for each component (`scripts/test_<device>.py` exits 0).

- [x] Dual microphones — obsolete: reSpeaker now handles mic inputs and output path
- [ ] Sabrent USB audio — playback smoke test script

> DS3218 servo, NF-A6x25 fan, TMP117, stereo cameras, and Hailo-8 bring-up
> are functionally exercised by the running services. Formal smoke-test scripts
> (`test_servo.py`, `test_fan.py`, `test_tmp117.py`, `test_cameras.py`,
> `test_hailo.py`) may still need writing/cleanup.

---

## Phase 3 — Perception

- [x] **VAD (Voice Activity Detection)** — detect when someone is speaking
- [x] **Wake-word detection** — trigger phrase ("Hey Assistant" or similar)
- [x] **Streaming STT (Speech-to-Text)** — convert mic audio to text in real time

> Face/person detection (Hailo-8 SCRFD) and stereo depth localization are
> complete. Audio perception is blocked on reliable microphone input — verify
> mic hardware before starting.
>
> **Exit criteria:** ≤150 ms face detect latency (met); reliable wake-word at 2 m.

---

## Phase 4 — Assistant Logic

- [x] **Intent router** — classify utterance into action category
- [x] **Dialog / state manager** — track conversation context, multi-turn flow
- [x] **End-to-end voice round-trip** — wake → STT → intent → response → TTS

> TTS pipeline and Sabrent audio out are complete (Phase 2 `av_service`).
> This phase is blocked on Phase 3 audio perception.

---

## Phase 5 — Animation & Personality

- [ ] **Idle micro-movements** — subtle random motion when no face is detected (random motion exists but needs refinement and personality tuning)
- [ ] **Audio-reactive head motion** — head moves slightly in sync with TTS output

> Gaze tracking (pan servo follows detected face) is complete.

---

## Phase 6 — Integration & Packaging

- [ ] **Update / rollback script** — `scripts/update.sh` that pulls, bumps, restarts
- [ ] **User-facing documentation** — README how-to for end-user operation
- [ ] **Cold-boot soak test** — verify boot-to-ready < 60 s; 24 h uptime test

> systemd units (`desktop-assistant-core`, `desktop-assistant-thermal`) and
> config layout are complete. `/etc/desktop-assistant/` path may need review
> vs current `config/` layout.

---

## Future Upgrades

- [ ] **Train custom "Hey VERA" wake word** — replace the bundled `hey_jarvis_v0.1`
  model with a model trained on the phrase "vera" or "hey vera".

  **Why:** "Hey Jarvis" works but is an awkward activation phrase for a robot named VERA.
  A custom model trained on VERA's acoustic environment will also have fewer false triggers.

  **Approach (hybrid — recommended):**
  1. Record 5–10 short WAV clips of "hey vera" spoken naturally on the Pi:
     `da record 10` (say the phrase 2–3× per clip, vary speed and inflection).
  2. On a dev machine (GPU preferred, CPU workable), install training deps:
     `pip install openwakeword[train]`
  3. Train with real clips + synthetic augmentation:
     ```
     python -m openwakeword.train \
       --phrase "hey vera" \
       --positive_reference_clips ./my_recordings/ \
       --output_dir ./hey_vera_model \
       --n_samples 5000
     ```
  4. Copy the output `.onnx` to:
     `~/.local/lib/python3.x/site-packages/openwakeword/resources/models/hey_vera_v0.1.onnx`
  5. Update `config/assistant.yaml`: `oww_model: hey_vera_v0.1`
  6. Restart the daemon: `sudo systemctl restart desktop-assistant-core`

  **Fallback:** Synthetic-only training (omit `--positive_reference_clips`) takes
  ~20 min and produces a usable model without any recordings. Accuracy ~80–90%.

  **Alternative phrases also available without training:** `hey_marvin_v0.1`,
  `hey_mycroft_v0.1` (already bundled). Switch by changing `oww_model` in config.

- [ ] **Silent servo replacement** — swap DS3218 for Feetech STS3032 or Dynamixel
  XL430-W250 (half-duplex TTL UART, near-silent, position feedback built in).
  Requires `src/hardware/servo.py` rewrite + Pi hardware UART freed up.
  See `PROJECT_PHASES.md § Future Upgrades` for wiring and library details.

## Waveshare ESP32-C6 LCD

Implementation backlog for the [Waveshare ESP32-C6-LCD-1.47](https://www.waveshare.com/product/esp32-related/boards-kits/esp32-c6/esp32-c6-lcd-1.47.htm).
The LCD process runs as Arduino C++ firmware on the ESP32 and communicates with
VERA through the established BLE GATT path.

- [x] **Define hardware and display requirements** — target the non-touch
  ESP32-C6 board with its 1.47-inch 172×320 ST7789 SPI TFT and BLE 5. The
  primary UX is an animated emotional mouth (neutral, listening, speaking,
  happy, sad, surprised, and error); the secondary UX is compact startup,
  restart, version, ready, degraded, and error text/iconography. Confirm exact
  GPIO mapping and display orientation from the board schematic during
  implementation. See the
  [manufacturer documentation](https://docs.waveshare.com/ESP32-C6-LCD-1.47).
- [x] **Create the managed Arduino firmware project** — add the ESP32 Arduino
  source, sketch metadata, board configuration, library dependencies, and
  project documentation under `firmware/vera_display`.
- [ ] **Establish the Arduino CLI toolchain** — use the latest suitable Arduino
  CLI workflow to install/configure the ESP32 board core and required libraries,
  with reproducible project-local build commands.
- [ ] **Implement the ESP32 BLE GATT display protocol** — add advertising,
  service/characteristic handling, framing, validation, reconnect behavior, and
  parsing compatible with `DisplayService`.
- [ ] **Implement the expressive mouth renderer** — render the primary animated
  emotional mouth states with smooth timing and a safe neutral/error fallback.
- [ ] **Implement startup/status rendering** — render concise boot progress,
  service status, ready, degraded, version, and error information as text or
  icons appropriate to the display resolution.
- [ ] **Automate firmware build and upload** — provide Arduino CLI commands or
  scripts for compiling and, when the device is connected and permissions
  allow it, uploading firmware to the ESP32. Surface missing port, board,
  dependency, and permission errors explicitly.
- [ ] **Integrate host events with the ESP32 firmware** — extend the host
  `DisplayService` and configuration to send mouth/emotion commands and
  startup/status messages while preserving disabled/unconfigured behavior.
- [ ] **Validate the Waveshare device end to end** — test BLE discovery and
  reconnect, mouth animation, startup/status rendering, orientation, refresh
  performance, and failure recovery; document wiring, flashing, and operation.

## Audio
- [x] reSpeaker mic capture — RESOLVED in v1.41.0 via `src/audio/pw_input.py`
      (`PipeWireMicInput`): a non-blocking `pw-record` subprocess feeds the
      AudioCaptureService. Clean, bounded shutdown (restart ~4s). Config:
      `audio.default.input_device_name: pipewire`.

---

## Security & Privacy (Public-Deployment Hardening)

Items required before VERA could be safely exposed outside a trusted home network.

### 🔴 Critical

- [ ] **TLS / HTTPS** — API key and camera streams travel in plaintext. Deploy an
  nginx reverse proxy with Let's Encrypt (or a self-signed cert for LAN) in front
  of the FastAPI service. All HTTP → HTTPS redirect; WSS for WebSocket streams.
  `?key=` in URLs will still appear in logs — move to cookie or Authorization header
  once TLS is in place.

- [ ] **Brute-force / rate-limit protection** — no lockout on the auth middleware.
  Add per-IP request rate limiting (e.g. `slowapi`) and a lockout after N failed
  key attempts. Log failures.

- [ ] **Replace single API key with session auth** — one shared secret means no
  per-user accountability and no revocation granularity. Replace with
  username+password login → short-lived JWT (or secure httpOnly cookie).
  Eliminates the localStorage XSS risk.

### 🟠 High

- [ ] **Biometric data consent & retention policy** — face embeddings, thumbnails,
  and photos are stored indefinitely with no consent mechanism, retention window,
  or GDPR/CCPA right-to-delete workflow. Add configurable auto-expiry and a
  documented deletion path for bystander faces.

- [ ] **RBAC (read vs admin roles)** — all authenticated users currently have full
  access including hardware control (servo), face deletion, and system reboot.
  Define at minimum a `viewer` role (camera + face list read-only) and an `admin`
  role (all operations).

- [ ] **Audit logging** — no record of who accessed what, when, or from where.
  Log all authenticated requests (timestamp, IP, route, HTTP status) to a
  tamper-evident append-only log file. Essential for breach detection.

- [ ] **Telegram bot command confirmation** — destructive commands (reboot,
  shutdown, face-clear) issued via Telegram have no secondary confirmation.
  Require a confirmation reply before executing.

### 🟡 Medium

- [ ] **OpenClaw API key storage** — `~/.openclaw/api-keys.env` is readable by any
  process running as `starter`. Move to a secrets manager or at minimum into the
  same `/etc/desktop-assistant/secrets.env` (mode 600) pattern used for VERA keys.

- [ ] **WebSocket idle timeout** — authenticated WebSocket connections have no
  server-side idle timeout. Add a server-side ping/pong timeout (~5 min) to
  close abandoned connections.

- [ ] **CORS policy** — no explicit CORS headers. Lock `Access-Control-Allow-Origin`
  to the specific trusted origin(s) rather than leaving it open.

- [ ] **Sudoers `kill` scope** — current whitelist allows `kill` broadly. Restrict
  to specific PIDs or service-restart patterns only.

### 🔵 Low / Operational

- [ ] **API key rotation tooling** — key was generated once at install with no
  rotation mechanism. Add a `vera key rotate` CLI command that generates a new
  key, updates `secrets.env`, restarts services, and prints the new key.

- [ ] **Suppress version disclosure** — TTS boot announcement and `/health` endpoint
  expose the exact version string. Minor information disclosure; easy to gate behind
  auth or make configurable.
