# Audio Hardware Notes

## Signal Chain Design

Full design diagram (current vs proposed):
- Source: `hardware/audio/audio_signal_chain.dot`
- Renders: `audio_signal_chain.pdf` / `.png` / `.svg`

Regenerate with:
```bash
cd hardware/audio
dot -Tpdf audio_signal_chain.dot -o audio_signal_chain.pdf
dot -Tpng -Gdpi=144 audio_signal_chain.dot -o audio_signal_chain.png
dot -Tsvg audio_signal_chain.dot -o audio_signal_chain.svg
```

---

## Current Problems & Fixes

### Problem 1: Speaker too quiet (immediate fix applied)
| | Before | After |
|--|--|--|
| Speaker ALSA level | 30% / −26 dB | **84% / −6 dB** |
| Mic capture level | 100% / +23 dB | **74% / +14 dB** |
| Auto Gain Control | off | off (keep off) |

Persisted with `sudo alsactl store` — survives reboot.

### Problem 2: AC hum on output (hardware fix required)
**Root cause:** CM108 USB DAC shares ground with Pi 5 switching regulator via USB cable shield.
The 50/60 Hz hum couples into the analog output before the PAM8403.

**Fix:** Insert a **ground loop isolator** (audio transformer, ~$8–15) between the 3.5mm jack
and the PAM8403 input. Any of these work:
- Mpow Ground Loop Noise Isolator (Amazon ~$10)
- Aukey 3.5mm Noise Filter
- Any 1:1 audio isolation transformer module

Also add decoupling to the PAM8403 VCC pin:
- 470 µF electrolytic + 0.1 µF ceramic in parallel, as close to VCC/GND pins as possible

### Problem 3: Mic input terrible (hardware replacement required)
**Root cause:** Full analog chain — electret capsule → MAX4466 preamp → 3.5mm jack → CM108 ADC.
Multiple noise injection points:
1. Pi 5 3.3V rail (MAX4466 VCC) has switching regulator noise
2. Analog signal wire acts as antenna for PWM/USB/HDMI noise
3. CM108 mic ADC has ~60 dB SNR — poor for speech
4. Ground loops between mic circuit GND and USB device GND

**Recommended replacement — Option A (drop-in, no code changes):**
> **ReSpeaker USB Mic Array v2.0** (~$40, Seeed Studio)
> - 4× MEMS microphones in circular array
> - Onboard DSP: beamforming, echo cancellation, noise suppression
> - USB audio device — Pi sees it as a second capture card (card 3)
> - VERA's `AudioInput` auto-selects by device name substring
> - Set `device_name: "ReSpeaker"` in audio config or leave on default

**DIY alternative — Option B (I2S MEMS, better quality, more setup):**
> **ICS43434 or INMP441 MEMS breakout** (~$5–8)
> - Digital I2S output → no analog path, zero PSU noise
> - Wire to Pi 5 GPIO: BCK (bit clock), WS (word select), DIN (data in)
> - Requires device tree overlay: `dtoverlay=googlevoicehat-soundcard`
>   or a generic `i2s-mmap` overlay
> - SNR: 61 dB (INMP441) / 65 dB (ICS43434) vs ~40 dB effective on current MAX4466 chain

---

## Output: USB Audio Adapter (CM108-class)

Two adapters are known-good and interchangeable:

| Adapter                                  | USB ID    | ALSA descriptor                    |
|------------------------------------------|-----------|------------------------------------|
| Sabrent AU-MMSA / AU-EMAC                | 0bda:*    | `USB PnP Sound Device`             |
| Unitek Y-247A (C-Media CM108)            | 0d8c:*    | `USB Audio Device`                 |

| Property       | Value                                       |
|----------------|---------------------------------------------|
| Interface      | USB 2.0 (standard UAC1)                     |
| Sample rates   | 44.1 / 48 kHz                               |
| Output         | 3.5 mm TRS stereo                           |
| Driver         | Kernel `snd-usb-audio` (auto-loaded)        |
| Match strategy | `find_output_device()` matches any of:      |
|                | "USB Audio", "C-Media", "Sabrent"           |

### Speaker wiring (3-pin TRS, pre-wired)

| Wire color | Pin    | Connection                    |
|------------|--------|-------------------------------|
| White      | Tip    | Left speaker (+)              |
| Red        | Ring   | Right speaker (+)             |
| Black      | Sleeve | Both speakers' (−) joined     |

8 Ω speakers will be quiet directly from a USB DAC. A small **PAM8403**
class-D amp module between adapter and speakers fixes this.

### Verifying

```bash
lsusb | grep -iE 'audio|sabrent|c-media|unitek'   # adapter present?
aplay -l                                          # list playback devices
arecord -l                                        # list capture devices
python3 scripts/test_speaker.py                   # left / right / sweep test tones
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
`AudioOutput`, so all TTS is automatically routed through the USB
adapter when present (Sabrent, C-Media/Unitek, or any device whose
ALSA name contains "USB Audio").

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
