# Camera Hardware Notes

## Raspberry Pi Camera Module 3 Wide

| Property       | Value                          |
|----------------|-------------------------------|
| Sensor         | Sony IMX708                   |
| Resolution     | 11.9 MP (4608 × 2592)         |
| Video modes    | 1080p30, 720p60, 4K15          |
| FoV (Wide)     | ~120° diagonal                |
| Interface      | 15-pin MIPI CSI-2             |
| Connector      | 22-pin FPC (Pi 5 connector)   |
| Current slot   | **CSI-0** (index 0)           |
| Future slot    | CSI-1 (index 1) when second camera added |

### Wiring / Physical

- Pi 5 has **two CSI/DSI combo ports** labelled CAM/DISP 0 and CAM/DISP 1
- Camera Module 3 uses a **22-pin FPC cable** (narrower than the original 15-pin)
- Ensure the lock lever is fully closed on both ends (camera and Pi)
- Cable routing: gold contacts face the PCB on the camera side; face away from USB ports on the Pi side

### Software Requirements

- `picamera2` — pre-installed on Pi OS Bookworm; also available via pip
- libcamera stack (included in Pi OS Bookworm)
- `camera_auto_detect=1` must be in `/boot/firmware/config.txt` (default on Bookworm)

### Enabling (if needed)

```bash
sudo raspi-config
# → Interface Options → Camera → Enable
# or manually:
echo "camera_auto_detect=1" | sudo tee -a /boot/firmware/config.txt
sudo reboot
```

### Verify camera detected

```bash
libcamera-hello --list-cameras
# Expected output includes: imx708_wide
```

### Known Issues

- **picamera2 must be installed via apt, not pip.** It depends on the
  `libcamera` Python bindings which have no PyPI wheel. The project venv
  must be created with `--system-site-packages` so it can see the
  apt-installed `python3-picamera2`. `setup_pi.sh` does this automatically.
- On Pi 5 the camera port uses 22-pin FPC; **15-pin cables from older Pi models
  will not fit** without an adapter

### Driver

- `src/vision/camera.py` — `Camera` class, wraps `Picamera2`
- Defaults: 1280×720 @ 30fps, RGB888 format, index 0
- Simulation mode: returns black frames when hardware unavailable
- Context manager supported: `with Camera() as cam: ...`

### Bring-up Script

```bash
source ~/.venv-assistant/bin/activate
python scripts/test_camera.py
# Saves a test still to /tmp/camera_test.jpg
```
