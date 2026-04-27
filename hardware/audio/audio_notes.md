# Audio Hardware Notes

## Output: Sabrent USB Audio Adapter (AU-MMSA / AU-EMAC)

| Property       | Value                                |
|----------------|--------------------------------------|
| Interface      | USB 2.0 (standard UAC1)              |
| Sample rates   | 44.1 / 48 kHz                        |
| Output         | 3.5 mm TRS stereo                    |
| Driver         | Kernel `snd-usb-audio` (auto-loaded) |
| ALSA name      | "USB PnP Sound Device" / "Sabrent"   |

### Speaker wiring (3-pin TRS, pre-wired)

| Wire color | Pin    | Connection                    |
|------------|--------|-------------------------------|
| White      | Tip    | Left speaker (+)              |
| Red        | Ring   | Right speaker (+)             |
| Black      | Sleeve | Both speakers' (−) joined     |

8 Ω speakers will be quiet directly from the Sabrent. A small **PAM8403**
class-D amp module between adapter and speakers fixes this.

### Verifying

```bash
lsusb | grep -i sabrent          # should show 0bda:* or similar
aplay -l                          # list playback devices
arecord -l                        # list capture devices
python3 scripts/test_speaker.py   # left / right / sweep test tones
```

## Input: Microphones (TBD)

The driver (`src/audio/input.py`) uses the system default input by default.
Once you wire a specific mic, set `AudioInputConfig.device_name` to a
substring of its ALSA name, or pass an explicit `device_index`.

```bash
arecord -l
python3 scripts/test_microphone.py   # records 5 s to /tmp/mic_test.wav
```

## TTS: espeak-ng

Lightweight offline backend used in Phase 1. Install:

```bash
sudo apt-get install -y espeak-ng
```

The driver (`src/audio/tts.py`) renders to WAV and plays through
`AudioOutput`, so all TTS is automatically routed through the Sabrent
adapter when present.

```bash
python3 scripts/test_tts.py
```

Higher-quality voices (Piper, Mimic 3) can be swapped in later — the
public `TextToSpeech.say()` API stays stable.

## Spoken Version (FR-VR1 .. VR4)

`src/audio/version_announcer.py` ties `core.version.spoken_version()`
to the TTS layer:

- `announce_startup()` — boot-time greeting
- `announce_on_request()` — verbal-query response
- `maybe_handle(utterance)` — pattern-matches "what version", etc.,
  and speaks the answer if applicable

Reads `/VERSION` as the single source of truth (FR-VR3).

## Drivers in `src/audio/`

| File                       | Purpose                                  |
|----------------------------|------------------------------------------|
| `output.py`                | `AudioOutput` — playback via sounddevice |
| `input.py`                 | `AudioInput` — capture via sounddevice   |
| `tts.py`                   | `TextToSpeech` — espeak-ng backend       |
| `version_announcer.py`     | Spoken-version helper (FR-VR1..VR4)      |

All four fall back to **simulation mode** when their backend is missing,
so unit tests run without hardware.
