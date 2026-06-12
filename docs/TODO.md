# VERA — Remaining Work

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

## Audio
- [ ] reSpeaker mic capture: the XVF3800 array is held exclusively by PipeWire,
      so the only PortAudio capture route is the `pulse` device. `sd.rec()` on
      `pulse` can block indefinitely (ALSA-pulse plugin), which wedged service
      shutdown (stuck "deactivating" until SIGKILL). The mic DOES capture real
      audio when it works (verified: peak ~30894/32767). Needs a non-blocking
      PipeWire-native capture path (e.g. a `pw-record`/`pw-cat` subprocess feeding
      the AudioCaptureService, or sounddevice with a hard read timeout) before
      switching `audio.default.input_device_name` to `pulse`.
