sudo systemctl stop desktop-assistant-core.service
python3 scripts/test_microphone.py
sudo systemctl start desktop-assistant-core.service# VERA — Remaining Work

> Extracted from `PROJECT_PHASES.md`. Items already completed are omitted.
> Reference the phase docs for full context, exit criteria, and design notes.

---

## Phase 1 — Hardware Bring-Up

Smoke-test scripts for each component (`scripts/test_<device>.py` exits 0).

- [ ] Dual microphones — record + level meter smoke test
- [ ] Sabrent USB audio — playback smoke test script

> DS3218 servo, NF-A6x25 fan, TMP117, stereo cameras, and Hailo-8 bring-up
> are functionally exercised by the running services. Formal smoke-test scripts
> (`test_servo.py`, `test_fan.py`, `test_tmp117.py`, `test_cameras.py`,
> `test_hailo.py`) may still need writing/cleanup.

---

## Phase 3 — Perception

- [ ] **VAD (Voice Activity Detection)** — detect when someone is speaking
- [ ] **Wake-word detection** — trigger phrase ("Hey Assistant" or similar)
- [ ] **Streaming STT (Speech-to-Text)** — convert mic audio to text in real time

> Face/person detection (Hailo-8 SCRFD) and stereo depth localization are
> complete. Audio perception is blocked on reliable microphone input — verify
> mic hardware before starting.
>
> **Exit criteria:** ≤150 ms face detect latency (met); reliable wake-word at 2 m.

---

## Phase 4 — Assistant Logic

- [ ] **Intent router** — classify utterance into action category
- [ ] **Dialog / state manager** — track conversation context, multi-turn flow
- [ ] **End-to-end voice round-trip** — wake → STT → intent → response → TTS

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

- [ ] **Silent servo replacement** — swap DS3218 for Feetech STS3032 or Dynamixel
  XL430-W250 (half-duplex TTL UART, near-silent, position feedback built in).
  Requires `src/hardware/servo.py` rewrite + Pi hardware UART freed up.
  See `PROJECT_PHASES.md § Future Upgrades` for wiring and library details.

---

## Security & Privacy

- [ ] **[SECRETS]** `config/assistant.yaml:190` — Live Telegram bot token `8848705130:AAGWB3_…` hardcoded and committed to the repository; anyone with repo read access can hijack the bot or harvest chat messages.
- [ ] **[AUTH]** `config/assistant.yaml:154` / `src/services/web_service.py` — Web dashboard bound to `0.0.0.0:8080` with no authentication on any route; every API endpoint (TTS, servo, face management, settings) is open to any host on the LAN.
- [ ] **[AUTH]** `src/services/web_service.py:1097,1113` — `POST /api/system/reboot` and `POST /api/system/shutdown` execute `sudo reboot` / `sudo shutdown` with zero auth check; unauthenticated callers on the LAN can power-cycle the device.
- [ ] **[AUTH]** `src/services/web_service.py:517,544` — MJPEG camera streams (`GET /stream`, `GET /stream2`) served without authentication; anyone on the local network can watch the live camera feed.
- [ ] **[NETWORK]** `src/services/ipc_bridge.py:183-186` — ZMQ PUB and REP sockets bound without CURVE authentication or encryption; any local process can subscribe to all bus events (including face-identity data) or inject arbitrary bus commands via the unauthenticated REP socket.
- [ ] **[FILESYSTEM]** `src/services/ipc_bridge.py:61-62` — ZMQ IPC socket files placed at predictable `/tmp` paths (`/tmp/desktop-assistant.pub`, `/tmp/desktop-assistant.rep`) with `PrivateTmp=false` by design; local users can attach to these sockets without authentication.
- [ ] **[INJECTION]** `src/services/av_service.py:603,666` — `POST /api/audio/record` and `POST /api/audio/playback` accept a caller-supplied `path` field and call `Path(path).expanduser()` with no directory restriction; an attacker on the LAN can write WAV files to arbitrary filesystem paths accessible to the service user.
- [ ] **[PRIVACY]** `src/perception/face_registry.py:712-748` + `config/assistant.yaml:83` — Biometric ArcFace embeddings and face photo crops stored indefinitely in `~/.local/share/desktop-assistant/` with no automatic purge policy and no user consent mechanism before collection begins.
- [ ] **[PRIVACY]** `src/services/telegram_service.py:122` + `config/assistant.yaml:186-191` — Face identity data (names, greeting phrases) transmitted to Telegram remote servers with no user opt-in/consent flow; feature activates automatically when `enabled: true` and a token is present.
- [ ] **[FILESYSTEM]** `~/.local/share/desktop-assistant/` (mode 775) and `thumbs/` (mode 775) — Both directories are group-writable; any process running as the `starter` group can tamper with or delete the biometric face database and thumbnail images.
- [ ] **[FILESYSTEM]** `services/systemd/desktop-assistant-watchdog.service:24` — `NoNewPrivileges=false` allows the watchdog process (which invokes `sudo systemctl restart` and `sudo kill`) to gain new privileges via setuid binaries, weakening systemd sandboxing.
- [ ] **[NETWORK]** `src/skills/smart_home_skill.py:6,169` — SmartHomeSkill documents and accepts a plain `http://` Home Assistant base URL; the Bearer long-lived access token would be transmitted in cleartext over HTTP if the user follows the documented example.
- [ ] **[DEPS]** `requirements.txt` — All 16 package constraints use `>=` lower-bound-only version specifiers with no hash pinning and no `--require-hashes` enforcement in CI; vulnerable to dependency-confusion attacks and silent malicious package updates.
- [ ] **[AUTH]** `src/services/web_service.py:1082` — `POST /api/restart` invokes `sudo systemctl restart desktop-assistant-core.service` with no auth check; a distinct unauthenticated kill-switch alongside the already-flagged reboot/shutdown endpoints.
- [ ] **[AUTH]** `src/services/web_service.py:573,831` — Both `/ws` and `/ws/tracking-debug` WebSocket endpoints `await ws.accept()` for any client and continuously stream live face identities, bus events, and tracking-debug telemetry to anyone on the LAN with no auth or `Origin` check.
- [ ] **[AUTH]** `/etc/sudoers` (`starter ALL=(ALL) NOPASSWD: ALL`) — The service user has passwordless sudo to every command, so any RCE or command-injection bug in the unauthenticated FastAPI/ZMQ surface escalates immediately to full host root.
- [ ] **[AUTH]** `src/services/web_service.py` (FastAPI app, no `CORSMiddleware`, no CSRF token, no `Origin` validation) — Any browser on the same LAN that loads a malicious page can issue cross-site `POST /api/system/reboot`, `DELETE /api/faces`, `POST /api/faces/{id}/train`, etc., turning the no-auth dashboard into a drive-by attack target.
- [ ] **[PRIVACY]** `config/assistant.yaml:173-176` — Pianobar setup comments instruct the user to store their Pandora account email and password in plain text in `~/.config/pianobar/config`; the project documents cleartext credential storage rather than a keyring or env-var indirection.
- [ ] **[PRIVACY]** `src/services/telemetry_service.py:93-100,208` — Bus telemetry SQLite at `~/.local/share/desktop-assistant/telemetry.db` (currently ~178 MB) is retained with only a per-topic row cap and no global TTL or purge job; long-running deployments accumulate face/motion/perception traces with PII indefinitely.
- [ ] **[NETWORK]** `src/services/telegram_service.py:43,54` and `src/watchdog/watchdog.py:77` — Telegram bot token is embedded directly in the request URL path (`/bot{token}/sendMessage`); any TLS-terminating proxy, IDS, captive portal, or system access log along the path will log the token, broadening the secret's exposure beyond the repo.
