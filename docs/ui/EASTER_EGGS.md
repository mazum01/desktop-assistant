# VERA web UI easter eggs

> **Spoilers ahead.** This lists every Star Trek easter egg hidden in the web
> dashboard. Stop reading here if you'd rather find them yourself.

All eggs are cosmetic. They never call the API, speak, move the head, or
change a setting. Most replies appear in the LCARS **Computer** comm bar at the
bottom right, which hides itself after 6 seconds. The code lives in
[`src/web/static/js/core/eggs.js`](../../src/web/static/js/core/eggs.js). The
design rules for adding new eggs are in the
[LCARS design guide §4.1](LCARS_DESIGN_GUIDE.md#41-easter-eggs).

## Typed phrases

Type these anywhere on the page, with no spaces. They are ignored while focus
is in a text field, select, or other form control, and when Ctrl, Alt, or Cmd is
held.

### Visual effects

| Type | Effect |
|---|---|
| `engage` or `maximumwarp` | Warp jump: star streaks rush out from the centre. |
| `beammeup` or `energize` | Transporter: the workspace shimmers out and back in. |
| `redalert` | Red alert: a pulsing red frame for 10 s. **Esc** stands down. |
| `shieldsup` | A blue shield bubble glows around the screen. |
| `cloak` | The workspace fades out of view and back. |
| `torpedo` | A glowing photon torpedo crosses the screen. |
| `tribble` | Tribbles appear and multiply (up to 48), then get beamed to the Klingons. |
| `selfdestruct` | A 5-second countdown under red alert. **Esc** aborts it. |

### Computer replies

| Type | Reply |
|---|---|
| `makeitso` | Acknowledged. Making it so. |
| `earlgrey` (also completes `teaearlgreyhot`) | Replicator offline. |
| `computer` | Working. |
| `resistanceisfutile` | Borg signature detected; shields adapting. |
| `fascinating` | Indeed. |
| `khan` | Ceti Alpha V is not on our flight plan. |
| `livelong` | And prosper. 🖖 |
| `qapla` | Today is a good day to compute. |
| `hail` | Hailing frequencies open. No response. |
| `dammitjim` | I'm a doctor, not a desktop assistant. |
| `fourlights` | There are four lights! |
| `darmok` | Darmok and Jalad at Tanagra. |
| `scotty` | I'm givin' her all she's got, Captain! |
| `lcars` | What the acronym stands for. |
| `redshirt` | An unlucky ensign joins the away team. |
| `numberone` | Number One, you have the conn. |
| `wesley` | Shut up, Wesley. |

## Keyboard sequence

| Keys | Effect |
|---|---|
| ↑ ↑ ↓ ↓ ← → ← → B A (Konami code) | Red alert. **Esc** stands down. |

## Hidden click targets

Click quickly; the count resets after 1.5 s without a click.

| Target | Clicks | Reply |
|---|---|---|
| Stardate in the left rail | 5 | A captain's log entry for the current stardate. |
| "V" logo in the left rail | 7 | Auto-destruct request denied. |
| Header code `47-0821 / 1701` | 3 | "NCC-1701. No bloody A, B, C or D." |
| Health badge (top right, e.g. "OK") | 3 | Level-1 diagnostic report with the current status. |

## Secret routes

Open these URLs (for example `http://vera3:8080/#/holodeck`). The computer
replies, then the router falls back to Home.

| Route | Reply |
|---|---|
| `#/holodeck` | Holodeck 3 is in use. |
| `#/ten-forward` | Closed for a private function. |
| `#/warp-core` | Warp core stable at 98% efficiency. |
| `#/bridge` | You have the bridge. |
| `#/jefferies-tube` | Nothing to see here. |
| `#/sickbay` | Please state the nature of the medical emergency. |
| `#/transporter-room` | Pattern buffers nominal. |
| `#/shuttlebay` | Request clearance from the bridge. |
| `#/brig` | The brig is empty. Keep it that way. |
| `#/astrometrics` | Long-range sensors show nothing but the living room. |

## Ambient eggs

- **Scene data cascade** (Home → Vision Control): about 3% of the decorative
  numbers are Trek references, such as 1701 (Enterprise), 74656 (Voyager),
  1031 (Discovery), 2364 (TNG's first season), 41153 (TNG's first stardate),
  and the ever-present 47. The full list is `CASCADE_EGGS` in
  `js/pages/home.js`.
- **Holiday greetings**: once per day per browser, a greeting appears on
  First Contact Day (5 April) and Star Trek Day (8 September).
- **Browser console**: opening devtools shows an LCARS "Live long and
  prosper" banner.

## Accessibility

With the OS or browser set to reduce motion, the warp and torpedo effects are
skipped, and red alert, shields, and the comm bar appear without animation.
Tribbles are replaced by a single message.
