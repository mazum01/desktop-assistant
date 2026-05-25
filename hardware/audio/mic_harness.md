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
   in copper foil tape grounded **at the CM108 (TRS sleeve) end only**.
   Faraday cage with no ground loop.

### Grounding (two-point, no Pi-pin-6 access)

The Pi 5 GPIO header pin 6 isn't physically reachable for this harness,
so we use the **two** ground points that ARE available:

| End                | Local ground reference                        |
|--------------------|-----------------------------------------------|
| Amp side           | I²C-header GND pin (came with the 3.3 V tap)  |
| Receiver side      | 3.5 mm TRS sleeve (CM108 mic-in)              |

Rules:

- **Amp-side ground:** ALL decoupling cap negatives (C1/C2/C3), the LPF
  shunt cap (C_lp), and the MAX4466 GND pin tie ONLY to the I²C-header
  GND pin. Treat it as a local star.
- **Receiver-side ground:** the twisted-pair return wire AND the copper-foil
  drain wire both tie ONLY to the TRS sleeve. Treat it as a second local star.
- **Copper foil shield FLOATS at the amp end.** Trim foil flush, do not
  connect anything to it on that side.
- The two ground stars are joined internally by the Pi 5 PCB ground plane
  (CM108 USB shield ↔ Pi USB shell ↔ I²C-header GND). This is ONE long
  conductive path, not a loop, because the harness itself only contributes
  one return path between the two stars (the twisted-pair return wire).

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

## Schematic

ASCII schematic of the entire harness, drawn for readability:

```
                            POWER FILTER (at MAX4466 VDD)
                            ─────────────────────────────

  Pi 5 I²C header                                                MAX4466
     3.3 V  ●──╮                                                  module
              │                                                  ┌──────┐
              ◓  Ferrite (BLM18AG601SN1 bead, or 5-turn          │      │
              ◓  ring choke on FT37-43 toroid)                   │      │
              │                                                  │      │
              ●────[ 10 Ω ]──────┬────────┬────────┬─────● VDD──►│ VDD  │
                                 │        │        │             │      │
                              ═══╪═══  ═══╪═══  ═══╪═══          │      │
                              ┬ 10 µF  ┬ 100 nF  ┬ 10 nF         │      │
                              │ (C1)   │ (C2)    │ (C3)          │      │
                              │ tant.  │ X7R     │ C0G           │      │
                              ●        ●        ●                │      │
                              │        │        │                │      │
   I²C header  GND  ●─────────●────────●────────●────────────────● GND  │
                              (LOCAL STAR — amp side)            │      │
                                                  ┌──── mic+ ────│ MIC+ │
                                  electret ───────┤              │      │
                                  capsule         └──── mic− ────│ MIC− │
                                                                 │ GAIN │ (pot, set ~50×)
                                                                 │      │
                                                                 │ OUT  ●──┐
                                                                 └──────┘  │
                                                                           │
                                                                          [1 kΩ]
                                                                           │
                                            (LPF — at MAX4466 OUT) ────────┤
                                                                           │
                                                                          ═╪═
                                                                          ┬ 4.7 nF
                                                                          │  C0G  (C_lp)
                                                                          │
                                                              GND ────────┘
                                                              (same LOCAL STAR
                                                               as amp side)

                                                                           │
                                                              ┌────────────● (signal)
                                                              │
                            SHIELDED TWISTED PAIR  ≤ 30 cm    │
                            ─────────────────────────────    │
                                                              │
                              ╔═══════════════════════════════╪═════════════════╗
                              ║   ┏━━━━━━━━━━━━━━━━━━━━━━━━━━ ● ━ signal ━━━━━━┓║
                              ║   ┃  copper foil shield wraps              ┃   ║
                              ║   ┃  both conductors helically             ┃   ║
                              ║   ┃                                        ┃   ║
                              ║   ┗━━━━━━━━━━━━━━━━━━━━━━━━━━●━━ return ━━━┛   ║
                              ║   ╲                                            ║
                              ║    ╲ foil FLOATS at amp end                    ║
                              ║                                                ║
                              ║                drain wire ──┐                  ║
                              ║                             │                  ║
                              ╚═════════════════════════════╪══════════════════╝
                                                            │
                                                            ▼
                                                   ┌────────────────────┐
                                                   │  3.5 mm TRS plug   │
                                                   │                    │
                                          signal──►│ Tip                │
                                          return──►│ Sleeve  ◄── drain  │
                                          n/c   ──►│ Ring               │
                                                   └────────────────────┘
                                                            │
                                                            ▼
                                                   ┌────────────────────┐
                                                   │  CM108 USB Audio   │
                                                   │       MIC IN       │
                                                   └────────────────────┘

  ═══ = capacitor      ◓ = ferrite bead / ring      [ ] = resistor       ● = junction
  ━━━ = conductor      ╔ ╗ ║ ╚ ╝ = shielded boundary
```

A node-graph version of the same wiring is in `mic_harness.dot/pdf/png/svg`
(letter-size, printable).

---

## Build form factor — inline or board?

Both work. Choose based on your skill level and available space:

### Option A — Inline (dead-bug, heat-shrink construction)

**Best for:** quick build, no PCB on hand, ≤ 30 cm cable runs.

Solder components leg-to-leg in mid-air, slip 3–5 mm heat-shrink over each
joint, then 12 mm shrink over the whole assembly for strain relief.

```
Pi-3.3V wire ──[ferrite bead in-line]──[10 Ω]──┬── to MAX4466 VDD
                                                │
                                          C1 ──●── GND wire
                                          C2 ──●── GND wire
                                          C3 ──●── GND wire
```

Pros: no board, very low cost, fits anywhere.
Cons: physically fragile; lead lengths get longer, which hurts HF filtering;
ugly to inspect.

### Option B — Tiny perfboard (RECOMMENDED for the power filter only)

**Best for:** the power-decoupling cluster at the MAX4466 VDD pin.

A 15 × 20 mm scrap of perfboard with 3 caps and a resistor takes < 10 min
to build and lets you mount the caps with **< 2 mm** lead length to VDD —
which is the difference between mediocre and excellent HF filtering.

```
   +───[10 Ω]───+────────● to VDD (jumper to MAX module pin)
   │            │
   │           ═╪═ C1 (10 µF tantalum, "+" pad here)
   │            │
   │           ═╪═ C2 (100 nF X7R 0805 or thru-hole)
   │            │
   │           ═╪═ C3 (10 nF C0G/X7R)
   │            │
   +────────────●─── GND bus  ── to MAX module GND (jumper)
                              ── to I²C header GND wire
```

Mount the ferrite (or ring choke) ON the perfboard at the +V input, or as
the very first thing on the wire entering the board.

Pros: short, controlled lead lengths; mechanically robust; easy to inspect
and rework; supports the ring-choke option below.
Cons: requires a perfboard scrap + 15 min of soldering.

### Option C — Both

Put the **power filter** (ferrite/choke + 3 caps + R) on a tiny perfboard
*at the MAX4466 module*, and run the **LPF** (1 kΩ + 4.7 nF) as inline
heat-shrink on the MAX4466 OUT wire. This is what I'd build.

---

## Ferrite ring choke vs. inline ferrite bead

**Short answer: yes, a ring choke is fine — and usually better.** The
BLM18 bead has ~600 Ω impedance only above ~100 MHz. A small ring choke
(toroid) wound with 5–8 turns has higher impedance starting at much lower
frequencies, which is exactly where the Pi 5 switching noise lives
(1–10 MHz).

### Two ways to use a ring

#### 1. Differential-mode choke (simplest, equivalent replacement for the bead)

Pass **only the +3.3 V wire** through the ring. Each pass = one turn.
5–8 turns gives you ~10–50 µH of inductance — far more than a bead.

```
Pi 3.3 V wire ──╮
                │
              ╭─┴─╮              5–8 turns through the same ring;
              │   │   ring       you can usually fit 6–8 turns of
              ◯   ◯   ferrite    28 AWG wire through a 10 mm OD
              │   │   toroid     toroid before it fills up.
              ╰─┬─╯
                │
                └──→ [10 Ω] → caps → MAX4466 VDD
```

This drops differential noise (the ripple on +3.3 V referenced to GND)
straight into the impedance of the choke + cap network.

#### 2. Common-mode choke (better — kills BOTH differential and common-mode noise)

Pass **both** the +3.3 V wire AND the GND return wire through the same
ring, in the same direction, same number of turns:

```
Pi 3.3 V ──╮
           │
         ╭─┴─╮             Same direction = phase additive for
         │   │             common-mode currents (noise that appears
   ┌──→  ◯   ◯  ─→─┐       on both wires at the same time → choke
   │     │   │     │       blocks it).
   │     ╰─┬─╯     │
   │       │       │       Opposite direction would be wrong — that
Pi GND ────╯       └→ MAX  cancels common-mode rejection.
                      GND
```

Both wires must wind through the toroid the **same number of times** in
the **same rotational direction**. This is a true common-mode choke and
will dramatically reduce both the 60 Hz ground-loop hum AND the MHz-range
switching noise on the same component. Highly recommended.

### Toroid material suggestions

| Material  | Best frequency | Notes                                                                |
|-----------|----------------|----------------------------------------------------------------------|
| Type 43   | 1–50 MHz       | **Best for Pi switching noise.** Cheap, easy to find (FT37-43, FT50-43). |
| Type 31   | 1–500 MHz      | Slightly better at high end; fine for our use.                       |
| Type 73   | 100 kHz–10 MHz | Good lower-frequency reach if you also see 60 Hz issues.             |
| Type 77   | 50 kHz–1 MHz   | Use ONLY if you confirm hum is in this band; otherwise pick 43.      |

Cheap source: Amazon "FT37-43 ferrite toroid", ~$8 for a pack of 20.
Fair Rite Type 43 toroids are also stocked at Digikey.

### Drop-in BOM update

Replace the BLM18AG601SN1 bead row with **either**:

| Qty | Part                              | Value / Part #             | Notes                                  |
|-----|-----------------------------------|----------------------------|----------------------------------------|
| 1   | Ferrite bead, inline              | BLM18AG601SN1 (600 Ω/100 MHz) | Original spec, ≥200 mA                 |
| —   | OR —                              | —                          | —                                      |
| 1   | Ferrite toroid (ring), Type 43    | FT37-43 (or FT50-43)       | Wind 5–8 turns of supply wire through |
| —   | Use as common-mode choke (best)   | —                          | Wind 5–8 turns of BOTH +V and GND     |

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

### Step 5 — Ground both stars locally

There is no central Pi pin 6 access. Instead, you have **two** ground
points, and that's fine — as long as each cap/component only connects to
ONE of them:

**Amp side (tie ONLY to I²C-header GND pin):**
- MAX4466 GND pin
- C1, C2, C3 negative terminals (power decoupling)
- C_lp negative terminal (LPF shunt)

**Receiver side (tie ONLY to TRS sleeve):**
- Twisted-pair return wire (the "GND conductor" of the cable)
- Copper foil drain wire

The Pi's internal ground plane joins the two stars via the CM108 USB
shield. Do NOT add any extra wire between the two stars — that would
create a parallel return path and turn the system into a loop antenna.

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
