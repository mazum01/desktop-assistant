# Hailo-8 AI Accelerator — Hardware Notes

## Board

| Property       | Value                          |
|----------------|--------------------------------|
| Module         | Raspberry Pi AI HAT+ (26 TOPS) |
| Accelerator    | Hailo-8 (HM218B1C2FA)          |
| Compute        | 26 TOPS INT8                   |
| Interface      | PCIe Gen3 x1 → Pi 5 PCIe FPC   |
| PCI vendor ID  | 0x1e60 (Hailo Technologies)    |
| Power          | Drawn from Pi 5 5V via HAT     |

## Software Stack

| Layer            | Package        | Source |
|------------------|----------------|--------|
| Kernel driver    | `hailo-pci`    | apt — included in `hailo-all` |
| User runtime     | HailoRT        | apt — included in `hailo-all` |
| CLI tool         | `hailortcli`   | apt — included in `hailo-all` |
| Python bindings  | `hailo-platform` | pip (Phase 3 only) |
| Pi enablement    | `rpi-hailo-tappas` | apt (optional, demo apps) |

### Install

Following the [Pi AI Kit getting-started guide](https://www.raspberrypi.com/documentation/accessories/ai-kit.html):

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y hailo-all
sudo reboot
```

After reboot, verify:

```bash
hailortcli fw-control identify
# Expected: Board Name: Hailo-8, Arch HAILO8, firmware version, serial
```

### Pi 5 PCIe enablement

The Pi 5 PCIe lane must be enabled. The AI HAT+ install script normally does
this automatically; if not, edit `/boot/firmware/config.txt`:

```
dtparam=pciex1
```

Reboot, then `lspci` should show:

```
0000:01:00.0 Co-processor: Hailo Technologies Ltd. Hailo-8 AI Processor [1e60:2864]
```

## Driver / Probe

- `src/perception/hailo_probe.py` — three-layer probe:
  1. PCIe presence (via `lspci`)
  2. `hailortcli` on PATH
  3. Firmware identify call
- Returns `HailoStatus` dataclass; never raises
- `HailoStatus.fully_ready` — true iff all three layers pass
- `HailoStatus.degrade_reason()` — human-readable reason to fall back to CPU

## Per-project Imperative

Per `.github/copilot-instructions.md` §5 (Hardware Safety):

> **Hailo-8**: Gracefully degrade to CPU inference if accelerator is unavailable.

Callers must check `status.fully_ready` before issuing inference; if false,
log `status.degrade_reason()` and route to CPU paths.

## Bring-up Script

```bash
python3 scripts/test_hailo.py
# Exits 0 if ready, 1 if degraded (with explanation)
```

## Known Issues

- The first invocation after boot can take 1–2 s to enumerate; the probe
  uses a 10 s timeout on `identify` to absorb this.
- If you see "Failed to open device" but `lspci` shows the chip, the kernel
  module may not be loaded: `sudo modprobe hailo_pci` and re-test. If still
  failing, reboot.
