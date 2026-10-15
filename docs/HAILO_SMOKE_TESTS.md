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

## Results — v1.66.0 (2026-10-15)

### Hailo-10H (vera3, HailoRT 5.1.1)

Models: SCRFD-10G, ArcFace MobileFaceNet, scdepthv3 and YOLOv11l with on-chip
NMS (Model Zoo v5.1.0 Hailo-10H HEFs).

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
      "ms_per_frame": 28.2,
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
      "model": "/home/starter/Code/desktop-assistant/config/hailo/hailo10h/yolov11l.hef",
      "labels_on_sample": [
        "bus",
        "person"
      ],
      "ms_per_frame": 47.7,
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
      "ms_per_frame": 10.4
    }
  },
  "ok": true
}
```

#### Hailo-10H object model comparison

All candidates detect the same 80 COCO classes. Per-frame time includes
letterboxing and decoding (mean of 10 runs).

| Image | Model | ms/frame | Objects | Top confidences |
|---|---|---|---|---|
| dog | yolov8m | 33.0 | bicycle, dog, truck | bicycle 0.97, dog 0.96, truck 0.76 |
| dog | yolov11m | 33.8 | dog, bicycle, truck | dog 0.95, bicycle 0.95, truck 0.74 |
| dog | yolov11l | 44.4 | bicycle, dog, truck | bicycle 0.97, dog 0.94, truck 0.84 |
| giraffe | yolov8m | 40.9 | giraffe, zebra | giraffe 0.94, zebra 0.94 |
| giraffe | yolov11m | 43.1 | giraffe, zebra | giraffe 0.92, zebra 0.89 |
| giraffe | yolov11l | 47.4 | giraffe, zebra | giraffe 0.96, zebra 0.90 |
| horse | yolov8m | 34.3 | horse, dog, person | horse 0.96, dog 0.94, person 0.92 |
| horse | yolov11m | 35.9 | horse, dog, person | horse 0.94, dog 0.93, person 0.92 |
| horse | yolov11l | 43.2 | horse, person, dog | horse 0.94, person 0.93, dog 0.93 |
| kitchen | yolov8m | 37.6 | orange×5 | orange 0.95, orange 0.94, orange 0.90 |
| kitchen | yolov11m | 39.8 | orange×4 | orange 0.84, orange 0.84, orange 0.76 |
| kitchen | yolov11l | 45.1 | orange×5 | orange 0.95, orange 0.92, orange 0.87 |
| bus | yolov8m | 36.2 | person×4, bus | bus 0.94, person 0.94, person 0.91 |
| bus | yolov11m | 38.8 | person×4, bus | bus 0.91, person 0.91, person 0.91 |
| bus | yolov11l | 45.0 | person×4, bus | bus 0.94, person 0.93, person 0.90 |

YOLOv11l found the same objects as YOLOv8m/YOLOv11m on every image, with
equal or higher confidence than YOLOv11m, for ~8–10 ms more per frame.

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
      "model": "/home/starter/Code/Desktop Assistant/config/hailo/yolo26m.hef",
      "labels_on_sample": [
        "bus",
        "person"
      ],
      "ms_per_frame": 76.6,
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
      "ms_per_frame": 9.0
    }
  },
  "ok": true
}
```
