# TMP117 & NF-A6x25 — Hardware Notes

## TMP117 Temperature Sensor

- Manufacturer: Texas Instruments / SparkFun Qwiic breakout
- Accuracy: ±0.1 °C (–20 to 50 °C)
- Interface: I²C, default address **0x48** (JP1 open)
- Voltage: 1.8 – 5.5 V (Qwiic is 3.3 V)
- Qwiic connector: SDA on pin 3, SCL on pin 5, 3V3, GND

### Wiring
Connect via any Qwiic cable to the Pi's Qwiic/STEMMA port or directly:
```
Pi Header  →  TMP117 Qwiic
Pin 1 3V3  →  3V3
Pin 3 SDA  →  SDA
Pin 5 SCL  →  SCL
Pin 6 GND  →  GND
```

---

## Noctua NF-A6x25 PWM Fan

- Voltage: 5 V
- Max current: 0.05 A
- Connector: 4-pin PWM (pin 4 = PWM signal, 25 kHz)
- RPM signal: pin 3 (tachometer, optional)

### PWM Pin Assignment
Hardware PWM is required for stable 25 kHz operation.

| Pi GPIO | Pi header pin | Function |
|---------|---------------|----------|
| GPIO13  | Pin 33        | PWM1 — fan control signal |
| GPIO12  | Pin 32        | PWM0 — alternate |

Configure in `config/fan.yaml`.

### Wiring
```
Fan connector  →  Pi / supply
Pin 1 GND      →  GND (shared)
Pin 2 +5V      →  5 V supply rail (NOT Pi 5V pin — use external)
Pin 3 Tach     →  GPIO (optional, e.g. GPIO6)
Pin 4 PWM      →  GPIO13 (hardware PWM1)
```

### Duty Cycle → Behavior
| Duty (%) | Behavior       |
|----------|----------------|
| 0        | Fan off (Noctua stops cleanly) |
| 30       | Minimum audible speed |
| 60       | Normal cooling  |
| 100      | Maximum / fail-safe |

### Fail-Safe
If TMP117 read fails, fan is immediately set to **100% duty**.
Configured in `src/thermal/thermal_manager.py`.


### Known Issues / Pi 5 Notes
- **lgpio must be pip-reinstalled inside the venv** on Pi 5 / Bookworm.
  The `python3-lgpio` apt package does not bind correctly inside a venv.
  `setup_pi.sh` handles this with `pip install --force-reinstall lgpio`.
