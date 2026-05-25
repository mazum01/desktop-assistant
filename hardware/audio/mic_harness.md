# VERA Microphone Wiring Harness — Filtered & Shielded Build

**Version:** 1.24.0
**Goal:** Eliminate AC mains hum (50/60 Hz), Pi 5 switching-rail noise
(1–2 MHz), and RF pickup (Wi-Fi, BT, USB clocks) from the MAX4466 + electret
mic chain feeding the CM108 USB audio dongle.

See `mic_harness.dot` / `.png` / `.svg` / `.pdf` for the schematic.

---

## Root causes of current noise

| Symptom              | Cause                                                                                                |
|----------------------|------------------------------------------------------------------------------------------------------|
| Low-frequency hum    | Long unshielded signal wire picks up 50/60 Hz mains field; ground loop via USB to wall adapter.       |
| High-pitch whine     | Pi 5 3.3 V rail switching ripple (PAM regulator @ ~2 MHz) couples directly into MAX4466 VDD.          |
| "Fuzzy" hiss         | MAX4466 input stage amplifies its own noise floor + bias-resistor Johnson noise; gain set too high.   |
| Intermittent clicks  | RF pickup from Wi-Fi / USB clocks; lack of input-side RF filter.                                      |
| Quiet signal overall | ALSA mic gain at 100 % was clipping the noise floor, not the speech. Reduced to 74 % already.        |

---

## Design overview

Three independent fixes in one harness:

1. **Power filter** at the MAX4466 VDD pin — ferrite bead + RC + 3 parallel
   decoupling caps. Removes Pi 5 switching noise BEFORE it gets into the amp.
2. **Output low-pass** on the MAX4466 OUT pin — 1 kΩ series + 4.7 nF shunt.
   Cuts everything above 34 kHz so RF can't ride on the analog audio line.
3. **Shielded twisted-pair cable** — signal + GND twisted together, wrapped
   in copper foil tape grounded **at the CM108 end only**. Faraday cage with
   no ground loop.

All grounds tie at a single **star ground** point at the Pi 5 GPIO Pin 6
(0 V). Avoid daisy-chaining grounds.

---

## Bill of Materials

| Qty | Part                                | Value / Part #                 | Notes                                       |
|-----|-------------------------------------|--------------------------------|---------------------------------------------|
| 1   | Ferrite bead, SMD 0805 (or thru)    | BLM18AG601SN1 (600 Ω @ 100 MHz)| Murata BLM18A series, ≥200 mA rating        |
| 1   | Resistor 1/8 W                      | 10 Ω, 1 %                      | Series with ferrite, forms RC with C1       |
| 1   | Capacitor, electrolytic or tantalum | 10 µF / 10 V                   | Bulk decoupling. Tantalum preferred.        |
| 1   | Capacitor, ceramic X7R              | 100 nF / 16 V                  | Mid-frequency decoupling                    |
| 1   | Capacitor, ceramic X7R or C0G       | 10 nF / 16 V                   | High-frequency decoupling                   |
| 1   | Resistor 1/8 W                      | 1 kΩ, 1 %                      | Series on MAX4466 OUT                       |
| 1   | Capacitor, ceramic C0G              | 4.7 nF / 50 V                  | LPF shunt to GND on OUT line                |
| 1   | Twisted-pair cable                  | 28–30 AWG, ≤ 30 cm             | Two-conductor; signal + return twisted ≥6 turns/inch |
| 1   | Copper foil tape (you have)         | Adhesive, 10–25 mm wide        | Wrap entire cable length                    |
| 1   | Drain wire                          | 24 AWG stranded                | Solder one end to copper foil + CM108 GND   |
| 1   | 3.5 mm TRS plug                     | Mono → CM108 MIC IN            | Tip = signal, ring/sleeve = GND             |

Total cost: ~$3–5 in parts (most of this you may already have).

---

## Build steps

### Step 1 — Decouple the MAX4466 VDD

Mount these components **as close as physically possible** to the MAX4466
VDD pin (within 5 mm). Lead length kills high-frequency filtering, so
short and direct is the rule.

```
Pi 5 3V3 ──[Ferrite]──[10 Ω]──┬──── MAX4466 VDD
                              │
                              ├── 10 µF ── GND
                              ├── 100 nF ─ GND
                              └── 10 nF ── GND
```

- Ferrite: bead the wire through it once; if SMD, solder in-line.
- All three caps in parallel: bulk (10 µF) handles slow ripple; mid (100 nF)
  handles audio-band noise; small (10 nF) handles RF. The combination has
  much lower impedance across the full spectrum than any single cap.

### Step 2 — Low-pass filter the MAX4466 output

```
MAX4466 OUT ──[1 kΩ]──┬──── to cable signal conductor
                     │
                     └── 4.7 nF ── GND
```

Cutoff frequency: fc = 1 / (2π · 1 kΩ · 4.7 nF) ≈ 33.9 kHz.
Speech goes to ~8 kHz, so this is fully transparent to your audio but
attenuates anything above 100 kHz by >9 dB. Stops RF cold.

### Step 3 — Build the shielded twisted-pair cable

1. Cut two lengths of 28–30 AWG insulated wire, ~5 cm longer than you need.
2. Strip ends. **Twist them together tightly** — at least 6 turns per inch.
   This rejects magnetic (common-mode) hum pickup.
3. Lay the twisted pair flat. Wrap **copper foil tape** around it in a
   helical wrap (each turn overlapping the previous by ~50 %). Cover the
   ENTIRE cable length.
4. At the **CM108 end** only: peel back ~5 mm of foil, solder a 24 AWG
   "drain wire" to it. This drain wire joins the cable GND at the 3.5 mm
   plug's sleeve.
5. At the **MAX4466 end**: trim the foil flush, do NOT connect it to
   anything. The shield "floats" here. ← This is the key rule.

### Step 4 — Wire the 3.5 mm plug

```
Plug Tip    ←── signal (from output of LPF)
Plug Sleeve ←── GND  +  copper foil drain wire
Plug Ring   ←── (not used on mono mic; tie to sleeve or leave open)
```

### Step 5 — Star-ground everything

All these grounds must converge at **one** physical point on the Pi 5
header (recommend Pin 6, GND, near the 3.3 V pin you tapped):

- MAX4466 GND pin
- All decoupling cap negatives (C1, C2, C3)
- The LPF shunt cap (C_lp) negative
- Copper foil drain wire (via the CM108 sleeve → USB shield → Pi USB GND)

Do **not** route the MAX4466 GND through a different path back to the Pi
than the CM108 ground takes. Two separate return paths = a loop area =
hum antenna.

### Step 6 — Mechanical routing

- Keep the cable **physically away** from:
  - Servo PWM wire (DS3218 — fast edges, big EMI)
  - Fan PWM wire (NF-A6x25 — switching @ 25 kHz)
  - USB power cables (especially the wall-adapter→Pi 5 cable)
  - Any AC mains wire or wall-wart
- If you must cross any of those wires, cross at a **90° angle**, not parallel.
- Cable runs over Pi 5 board: route over the GND pour, not over the SoC or
  USB chips.

---

## Verification

After install, with the daemon stopped:

```bash
# Capture 5 s of silence (no one speaking, no music)
arecord -D plughw:2,0 -d 5 -f cd /tmp/silence.wav

# Measure noise floor
sox /tmp/silence.wav -n stat 2>&1 | grep -E "RMS|Max amp"
```

| Metric                | Before harness | Target after harness |
|-----------------------|----------------|----------------------|
| RMS amplitude         | ~0.01–0.05     | < 0.001              |
| Maximum amplitude     | ~0.3           | < 0.01               |
| Audible 60 Hz hum     | yes            | no                   |
| Audible 2 MHz whine   | yes (as hiss)  | no                   |

Then with someone speaking at ~50 cm:

```bash
arecord -D plughw:2,0 -d 5 -f cd /tmp/voice.wav
sox /tmp/voice.wav -n stat 2>&1 | grep -E "RMS|Max amp"
```

You want voice RMS > 0.05 (signal/noise ratio better than 50× ≈ 34 dB).

---

## Common mistakes to avoid

1. **Don't ground the shield at both ends.** This creates a ground loop;
   the very thing you're trying to fix. One end only — at the receiver
   (CM108) end.
2. **Don't run the harness parallel to the fan PWM wire.** The fan PWM is
   a 25 kHz square wave with sharp edges — guaranteed audible interference.
3. **Don't omit the bulk cap (10 µF).** The MAX4466 has a 24 dB internal
   gain and *will* amplify any low-frequency ripple on its VDD pin into
   audible hum on its OUT pin.
4. **Don't set the MAX4466 gain pot to maximum.** Max gain is 200×. Use
   ~50× (turn the pot ~1/3 from minimum). You want headroom; software
   AGC can amplify further if needed.
5. **Don't use foil tape with an insulating adhesive between the foil
   and the wire.** The adhesive doesn't matter for shield function
   (capacitive shield, not resistive), but you DO need the drain wire to
   make conductive contact with the foil itself — solder, don't just tape.

---

## Upgrade path (future)

Even with all the above, the MAX4466 + electret combo has an absolute
noise floor of ~−90 dBV (≈30 µV). A digital MEMS mic (e.g., ICS43434
I²S or ReSpeaker USB Mic Array v2.0) bypasses this entire analog chain
and gets you to −110 dBV with built-in beamforming and AEC. See
`audio_notes.md` § "Replacement mic options" for the BOM.
