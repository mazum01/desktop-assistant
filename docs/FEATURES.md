# VERA Feature Parity Matrix

This document tracks which features are accessible via each interface:
**CLI** (`vera` / `da`), **Web GUI** (`http://localhost:8080`), and
**OpenClaw** (Telegram natural-language skill).

✅ = Available  ❌ = Not available  🔧 = Partial / limited

---

## Vision & Tracking

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Face detection | ✅ (auto) | ✅ (camera feed overlay) | ✅ describe-scene |
| Face tracking (servo follows face) | ✅ `vera face-tracking` | ✅ Settings panel | ✅ face-tracking |
| Person seek (body tracking fallback) | ✅ `vera person-seek` | ✅ Settings panel | ✅ person-seek |
| Random idle motion | ✅ `vera random-motion` | ✅ Settings panel | ✅ random-motion |
| Object detection (Hailo COCO) | ✅ `vera object-detection` | ✅ Settings panel | ✅ object-detection |
| Scene description (LLM) | ✅ `vera vision describe` | ❌ | ✅ describe-scene |
| Privacy / nudity detection | ✅ `vera privacy` | ✅ Settings panel | ✅ privacy |
| Camera pan (servo) | ✅ `vera servo pan` | ✅ Servo slider | ✅ pan-camera |
| Camera snapshot | ✅ `vera snapshot` | ✅ Camera feed | ✅ grab-frame |
| Camera resolution (cam1) | ✅ `vera camera resolution` | ❌ | ❌ |
| Camera rotation | ✅ `vera camera rotation` | ❌ | ❌ |

---

## Depth Estimation

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Dense stereo depth (SGBM) | ✅ `vera depth dense-enable/disable` | ✅ Depth panel | ✅ depth-toggle |
| Monocular neural depth (Hailo) | ✅ `vera depth mono-enable/disable` | ✅ Depth panel | ✅ depth-toggle |
| Depth query (distances) | ✅ `vera depth query` | ✅ Depth panel | ✅ depth-query |
| Stereo calibration | ✅ `scripts/calibrate_stereo.py` | ❌ | ❌ |

---

## Face Registry

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Name a face | ✅ `vera face meet` | ✅ Face panel | ✅ faces |
| List faces | ✅ `vera face list` | ✅ Face panel | ✅ faces |
| Refresh embeddings | ✅ `vera face refresh` | ✅ Face panel | ✅ faces |
| Forget all faces | ✅ `vera face forget-all` | ✅ Face panel | ✅ faces |
| Merge face identities | ✅ `vera face merge` | ❌ | ❌ |
| Greeting cooldown | ✅ `vera face greeting-cooldown` | ✅ Face settings | ❌ |

---

## Audio

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Speak text (TTS) | ✅ `vera say` | ❌ | ✅ say |
| Record audio | ✅ `vera record` | ❌ | ✅ record |
| Play back recording | ✅ `vera playback` | ❌ | ✅ playback |
| EQ preset | ✅ `vera eq set` | ✅ Audio panel | ❌ |
| Audio backend config | ✅ `vera audio` | ✅ Audio panel | ❌ |
| Volume | ✅ `vera music volume` | ✅ Music panel | ✅ music |

---

## Music (Pandora via pianobar)

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Play / stop | ✅ `vera music play/stop` | ✅ Music panel | ✅ music |
| Skip song | ✅ `vera music next` | ✅ Music panel | ✅ music |
| Pause / resume | ✅ `vera music pause` | ✅ Music panel | ✅ music |
| Station select | ✅ `vera music play <station>` | ✅ Music panel | ✅ music |
| Thumbs up/down | ✅ `vera music thumbs-up/down` | ✅ Music panel | ✅ music |
| List stations | ✅ `vera music stations` | ✅ Music panel | ✅ music |
| Now playing status | ✅ `vera music status` | ✅ Music panel | ✅ music |

---

## IoT / Smart Home

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Radon monitor | ✅ `vera radon` | ❌ | ✅ radon |
| DROP water softener | ✅ `vera drop` | ❌ | ✅ drop |
| IoT plugin CRUD | ✅ `vera iot` | ✅ IoT panel | ❌ |
| Yale lock | ✅ `vera lock` | ✅ Lock panel (via IoT) | ❌ |
| Nest thermostat | ❌ | ✅ Nest card | ❌ |

---

## System & Thermal

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| System status / health | ✅ `vera status` | ✅ Status bar | ✅ system-status |
| Fan override | ✅ `vera fan set/auto` | ✅ Fan panel | ❌ |
| Fan curve (control points) | ✅ `vera fan curve-set` | ✅ Fan panel | ✅ fan-control |
| Fan tachometer | ✅ `vera fan tach-status` | ✅ Fan panel | ❌ |
| Temperature blend | ✅ (via config) | ✅ Fan panel | ❌ |
| Announce time | ✅ `vera time` | ❌ | ✅ time |
| Software version | ✅ `vera system version` | ✅ About footer | ✅ version |
| Reboot / shutdown | ✅ `vera system reboot/shutdown` | ✅ Settings | ✅ power |
| Restart daemon | ✅ `vera system restart` | ✅ Settings | ❌ |

---

## Quiet Hours

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Enable / disable | ✅ `vera quiet-hours enable/disable` | ✅ Settings panel | ✅ quiet-hours |
| Configure window | ✅ `vera quiet-hours set` | ✅ Settings panel | ✅ quiet-hours |
| Status | ✅ `vera quiet-hours status` | ✅ Settings panel | ✅ quiet-hours |

---

## Skills (OpenClaw voice)

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| List skills | ✅ `vera skills list` | ✅ Skills panel | ❌ |
| Enable / disable skill | ✅ `vera skills enable/disable` | ✅ Skills panel | ❌ |
| Configure skill | ✅ `vera skills config` | ✅ Skills panel | ❌ |

---

## Room Awareness

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Get current room | ✅ `vera room get` | ✅ Status bar | ❌ |
| Set room | ✅ `vera room set` | ✅ Settings | ❌ |
| Auto room detection | ✅ (auto, LLM) | ✅ (auto) | ❌ |

---

## Miscellaneous

| Feature | CLI | Web | OpenClaw |
|---------|-----|-----|----------|
| Tell a joke | ✅ `vera joke` | ❌ | ✅ joke |
| Email monitoring | ❌ | ❌ | ✅ email-monitor |
| Bus topic monitor | ✅ `vera watch` | ❌ | ❌ |
| Publish raw bus event | ✅ `vera publish` | ❌ | ❌ |
| Face greeting (auto) | ✅ (auto) | ✅ (auto) | ❌ |
| Guest intro cooldown | ✅ `vera face greeting-cooldown` | ✅ Face settings | ❌ |

---

*Last updated: 2025-06-01. See [CHANGELOG.md](../CHANGELOG.md) for version history.*
