# Hailo model profiles

VERA supports Hailo-8 (HailoRT 4.x VStreams) and Hailo-10H
(HailoRT 5.x InferModel). HEFs are architecture-specific: never copy an
Hailo-8 HEF into a Hailo-10H profile.

## Install

Install the device's HailoRT driver and Python bindings first, then run from
the repository root:

```bash
python3 -m src.perception.hailo_models --install
```

The Pi setup script also runs this step when `hailortcli` is installed.
Downloads are validated with `hailortcli parse-hef` before being installed.
Large binaries are ignored by Git. Existing compatible models in legacy or
system-package locations are reused.

## Selection and tuning

[`models.yaml`](models.yaml) is the source of truth for face detection,
face embeddings and mono depth. `device: auto` probes the accelerator.
For a manually selected profile, set `device` to `Hailo-8` or `Hailo-10H`.
This selects model files; it does not make a HEF compatible with different
silicon or replace the runtime's hardware probe.

| Function | Hailo-8 | Hailo-10H |
| --- | --- | --- |
| Faces | SCRFD-10G | SCRFD-10G, SCRFD-2.5G fallback |
| Recognition | ArcFace MobileFaceNet | ArcFace MobileFaceNet |
| Mono depth | scdepthv3 | scdepthv3 |
| Objects | Existing YOLO26m/n + ONNX postprocessing | Existing YOLOv8m/YOLOv11m with on-chip NMS |

The profile's `file` and `url` choose the downloadable model; `candidates`
lists compatible alternatives. Installed files under
`config/hailo/hailo8/` or `config/hailo/hailo10h/` take precedence over
candidate paths. To select a smaller SCRFD, change `file` and `url` to the
matching architecture's `scrfd_2.5g.hef`, then rerun the installer.
Use the same ArcFace architecture to retain compatibility with existing
512-dimensional face galleries; a different embedding network may require
re-enrolling identities.

SCRFD scores are already probabilities. Both Python and native decoders
threshold them directly. Rebuild the optional native extension after updating:

```bash
(cd src/perception/scrfd_decode && python3 setup.py build_ext --inplace)
```

An older extension is ignored safely in favor of the Python decoder.
Hailo-10H outputs are dequantized to float32 before postprocessing.
Configuration and synchronous inference share a lock to prevent startup
races as multiple models are loaded.

## Enable mono depth

In [`assistant.yaml`](../assistant.yaml):

```yaml
depth:
  mono_enabled: true
  mono_hef_path: auto
  mono_rate_hz: 3.0
```

An explicit `mono_hef_path` still overrides automatic selection, including
legacy Hailo-8 installations. Restart the core service after changing model
files or startup configuration. Use `systemctl --user` for a user-scoped
installation and `sudo systemctl` for a system-scoped installation.

The authenticated dashboard endpoint `/api/depth/mono/stats` reports
`hardware_ready`, shape, range and NaN count; `/api/depth/mono` displays the
depth image. A successful enable flag alone is not proof of inference.
The map is **relative depth, not calibrated metres**. Metric estimates
require an explicitly calibrated `mono_scale_factor`.

Missing face models fall back to CPU Haar detection; missing ArcFace or
depth models are logged and those capabilities remain unavailable.
