# Hailo hardware smoke tests

`scripts/hailo_smoke_test.py` runs every Hailo-backed perception model on the
attached accelerator and prints a JSON report (exit code 1 on any failure):

| Check | Pass criteria |
|---|---|
| `face_detection` | Hailo backend; 1 face on sample image, 0 on blank and noise |
| `face_embedding` | Hailo ArcFace; same-face cosine similarity > 0.8 |
| `object_detection` | Hailo backend; `person` and `bus` found on `bus.jpg` |
| `mono_depth` | Hailo scdepthv3; finite, non-constant depth map |

The core service holds the accelerator, so stop it first:

```bash
# Hailo-10H (vera3, user units) — wait for the HailoRT server to release models
systemctl --user stop desktop-assistant-watchdog desktop-assistant-core; sleep 8
python3 scripts/hailo_smoke_test.py
systemctl --user start desktop-assistant-core desktop-assistant-watchdog
```

## Results — v1.65.1 (2026-10-15)

### Hailo-10H (vera3, HailoRT 5.1.1)

Models: SCRFD-10G, ArcFace MobileFaceNet, scdepthv3 (Model Zoo v5.1.0
Hailo-10H HEFs) and YOLOv8m with on-chip NMS.

```json
{
  "device": "Hailo-10H",
  "checks": {
    "face_detection": {
      "backend": "hailo",
      "model": "/home/starter/Code/desktop-assistant/config/hailo/hailo10h/scrfd_10g.hef",
      "faces_on_sample": 1,
      "faces_on_blank": 0,
      "faces_on_noise": 0,
      "ms_per_frame": 27.9,
      "ok": true
    },
    "face_embedding": {
      "hardware": true,
      "model": "/home/starter/Code/desktop-assistant/config/hailo/hailo10h/arcface_mobilefacenet.hef",
      "same_face_similarity": 0.964,
      "ok": true
    },
    "object_detection": {
      "backend": "hailo",
      "labels_on_sample": [
        "bus",
        "person"
      ],
      "ms_per_frame": 33.5,
      "ok": true
    },
    "mono_depth": {
      "model": "/home/starter/Code/desktop-assistant/config/hailo/hailo10h/scdepthv3.hef",
      "ok": true,
      "hardware": true,
      "shape": [
        256,
        320
      ],
      "finite": true,
      "std": 0.2245,
      "ms_per_frame": 10.5
    }
  },
  "ok": true
}
```

### Hailo-8 (vera.local, HailoRT 4.23)

Models: SCRFD-10G, ArcFace MobileFaceNet, scdepthv3 and YOLO26m (+ONNX
post-processing). Mono depth runs on hardware but is left disabled in this
host's local config.

```json
{
  "device": "Hailo-8",
  "checks": {
    "face_detection": {
      "backend": "hailo",
      "model": "/usr/local/hailo/resources/models/hailo8/scrfd_10g.hef",
      "faces_on_sample": 1,
      "faces_on_blank": 0,
      "faces_on_noise": 0,
      "ms_per_frame": 8.1,
      "ok": true
    },
    "face_embedding": {
      "hardware": true,
      "model": "/usr/local/hailo/resources/models/hailo8/arcface_mobilefacenet.hef",
      "same_face_similarity": 0.971,
      "ok": true
    },
    "object_detection": {
      "backend": "hailo",
      "labels_on_sample": [
        "bus",
        "person"
      ],
      "ms_per_frame": 75.7,
      "ok": true
    },
    "mono_depth": {
      "model": "/usr/local/hailo/resources/models/hailo8/scdepthv3.hef",
      "ok": true,
      "hardware": true,
      "shape": [
        256,
        320
      ],
      "finite": true,
      "std": 0.2214,
      "ms_per_frame": 9.1
    }
  },
  "ok": true
}
```
