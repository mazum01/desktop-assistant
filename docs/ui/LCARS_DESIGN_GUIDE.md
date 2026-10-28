# VERA LCARS Design Guide

This guide is the reference for every element of VERA's web UI. Read it before
you add or change UI, and update it when you introduce a new component.

> **About the source material.** LCARS (Library Computer Access/Retrieval
> System) is the fictional computer interface that Michael Okuda designed for
> *Star Trek: The Next Generation*. It is Paramount/CBS intellectual property.
> VERA uses an *LCARS-inspired* homage for personal, non-commercial use. This
> document is an original summary of the visual conventions. No artwork,
> templates, fonts, or text have been copied from the sources listed at the end.

---

## 1. Core principles

1. **Black field.** Everything sits on pure black (`--bg: #000`). Black is also
   what separates one element from the next.
2. **Flat, solid color.** No gradients, drop shadows, glows, bevels, or outline
   borders on LCARS elements. A shape is defined by its fill, not its stroke.
3. **Shapes are bars, elbows, and pills.** UI is built from these:
   - horizontal bars;
   - vertical spines;
   - quarter-round *elbows* (sweeps) that join a spine to a bar;
   - blocks rounded on one or both ends.

   Only the *outer* end of a run of blocks is rounded. The blocks inside the run
   stay square.
4. **Black gaps instead of borders.** Adjacent blocks are separated by a
   `--lcars-gap` (6px) strip of black, never by a drawn line.
5. **Condensed uppercase labels.** Control labels use `--console-font` (Barlow
   Condensed), uppercase, with slight letter-spacing.
6. **Black text on color.** A label on a colored block is black. Light text is
   only for content on the black field, such as readouts and prose.
7. **Labels sit low and right.** Inside a block, the label hugs the bottom-right
   corner (`align-items: flex-end; justify-content: flex-end`).
8. **Color means something.** Each workspace owns one hue. Status colors (green,
   red, ice) are used only for status.
9. **Decoration is sparse.** Numeric "codes" such as `47-0821 / 1701` add
   flavor. Mark them `aria-hidden="true"` and keep the count low; the layout
   test allows only 3–6 `lcars-code` elements.

## 2. Tokens (from `src/web/static/style.css :root`)

Always use these tokens. Never hard-code a new color or dimension; add a token
here and in `:root` first.

### Color

| Token | Value | Use |
|---|---|---|
| `--bg` | `#000` | Page field, gaps |
| `--bg2` / `--bg3` | `#09090e` / `#18151f` | Card interiors, inputs |
| `--text` / `--text-dim` | `#f4e9df` / `#c3b5c9` | Prose and readouts on black |
| `--orange` | `#ffad66` | Primary frame, active control |
| `--lcars-peach` / `--yellow` | `#ffcc99` | Active mode, highlights |
| `--blue` | `#a5b8ff` | Secondary frame |
| `--purple` | `#c8a9ef` | Tertiary frame |
| `--lcars-lilac` | `#cc99cc` | Accent blocks |
| `--lcars-ice` | `#99ccff` | **Lit** toggle, cross-links, data text |
| `--lcars-dim` | `#8a7aa0` | **Unlit** toggle, inactive segment |
| `--green` | `#a3d9b1` | Healthy / OK |
| `--red` | `#ff8c85` | Fault / degraded / Engineering mode |
| `--lcars-line` | `#30283c` | Hairline dividers *inside* cards only |

### Workspace hues (`--workspace-color`, applied via `body[data-workspace]`)

| Workspace | Hue |
|---|---|
| Home | `#ff9c63` |
| Overview | `--orange` |
| Vision & Motion (`perception`) | `#a5b8ff` |
| Media & Audio (`controls`) | `#ffcc99` |
| Voice & Behavior (`voice`) | `#c8a9ef` |
| Smart Home (`smart-home`) | `#df99bc` |
| System (`system`) | `#ccbb88` |
| Settings (`settings`) | `#99cccc` |

Cards take on the workspace hue through `--panel-accent`. A new workspace needs
both a hue here and a rail button.

### Dimensions

| Token | Value | Use |
|---|---|---|
| `--lcars-gap` | 6px | Black gap between blocks |
| `--lcars-bar-height` | 34px (28px on phones) | Header button bar |
| `--lcars-spine-width` | 42px | Rail spine |
| `--lcars-frame-spine` | 29px | Card / header spine |
| `--lcars-topbar-bar` | 10px | Horizontal bar of header and card frames |
| `--rail-width` | 208px | Navigation rail |
| `--header-height` | 78px | Header frame |
| `--console-font` | Barlow Condensed | All LCARS labels |

## 3. Components

Reuse these components; don't invent near-duplicates.

| Component | Class / location | Notes |
|---|---|---|
| **Frame** (elbow + spine + bar) | `header`, `.card` | The left spine sweeps into a top bar. Card titles are uppercase in the panel accent. |
| **Rail button** | `.tab-btn` | Full-color block with the label at bottom-right. Active state: `box-shadow: inset -6px 0 #fff`. |
| **Segmented button bar** | `.mode-toggle` + `header .badge` (health: just the state word in `.badge-value`; the block's hue and `aria-label="System health"` say what it is) | Square blocks split by gaps; only the outer ends are rounded. Active segment: peach with the rail buttons' white end highlight (`inset -6px 0 #fff`), never an underline. Use `aria-pressed`. |
| **Segmented selector** | `.lcars-segmented` > `button[aria-pressed]` | One-of-N setting inside a card (e.g. Settings → Display Units). Same blocks as the header bar at `--lcars-bar-height`, outer ends rounded, wraps on narrow screens. Lit choice: `--lcars-ice` + white end highlight. Don't reuse `data-mode-option` — that attribute belongs to the interface-mode bar. |
| **Lit/unlit toggle** | `.lcars-toggle` > `input` + `.lcars-toggle-face` | Block with the right end rounded. Unlit = `--lcars-dim`, lit = `--lcars-ice`. Always shows an `ON`/`OFF` readout. The native checkbox stays in the DOM, visually hidden but focusable. |
| **Readout** | `.stat-box` > `.stat-label` + `.stat-value` | Small uppercase label above a large value on black. |
| **Alert frame** | `section.lcars-alert-frame` > `.lcars-alert-title` + pills | Top bar sweeping down into left and right spines, open at the bottom. Shown only while there are messages; hue follows the worst alert. See §3.2. |
| **Sweep title** | `a.lcars-sweep-title` | A sweep card's name, cut into the left of its thick top bar as a black gap with text in the frame hue. It replaces the card's `h2` and doubles as the cross-link (ice on hover). Label the card with `aria-labelledby`. See §3.1. |
| **Sweep readout** | `.lcars-sweep-bar-top` > `.sweep-readout` | Read-only key/value block in a sweep card's top bar (no hover, not focusable). See §3.1. |
| **Status pill** | `.pill.pill-ok/-warn/-bad` | Short status words. Never color alone: the text must state the condition. |
| **Card cross-link** | `h2 > a.card-link` | Floats right in the card title, ice-colored, ends with `›`. Use it instead of an "Open X" button. Sweep cards and bar-title cards use their title as the link instead. |
| **Bar-title card** | `section.card.lcars-bar-title-card` > `h2.lcars-bar-title > a` | A standard card whose top bar is thickened to 22px (18px on phones) with the title cut into it, like a sweep title, instead of on its own row. The title link is the cross-link (accent, ice on hover), so there's no separate `›` link. Label the card with `aria-labelledby` on the link. Use it for prominent full-width cards where height matters (Home's Media Player); keep the sweep card for the one hero panel. |
| **Action button** | `.btn` | Existing rounded action buttons inside cards. Use them for verbs (Play, Say), not for state. |
| **Decorative code** | `.lcars-code` | `aria-hidden`, sparse (3–6 on the page). |
| **Sweep card** | `.card.lcars-sweep-card` + `.lcars-sweep-bar` | Card frame closed by a bottom elbow; the thick bottom bar carries controls. Used for live video. See §3.1. |
| **Mirrored sweep card** | `.lcars-sweep-card.is-mirrored` | Same frame with the spine, elbows and frame breaks on the **right**. Use it for the companion panel beside a sweep card so the pair reads as one bracketed console (Home's Vision + Vision Control). |
| **Sub-frame** | `div.lcars-subframe-stack` > `div.lcars-subframe` (`.is-scene` / `.is-behavior` / `.is-head`, or set `--subframe-hue`) > `span.lcars-subframe-title` + content | Inner groups hung off one right-hand vertical bar. The first group's top bar sweeps up out of the bar (rounded top-right elbow). Middle groups tee off it with square-cornered top bars. The last group's bar runs along its bottom and sweeps down into the bar (rounded bottom-right elbow). 4px black gaps between groups act as frame breaks. Each title is cut into its group's bar near the right-hand bar, as a black box with text in the group's hue. Give neighbouring sub-frames different hues (Home: Scene lilac, Behavior blue, Head position peach). Label the group with `aria-labelledby` pointing at the title. |
| **Data cascade** | `div.lcars-data-cascade[aria-hidden=true]` > `.lcars-cascade-col` > `span` | Decorative columns of random numbers, right-aligned, washed through dim → peach → ice → white → lilac by a stepped CSS animation staggered by row. It takes its height from its neighbours (`contain: size`), shows only whole rows and drops whole columns that don't fit. JS (`cascadeColumns` in `home.js`) fills it and refreshes the numbers every few seconds while the page is visible. At most one per page, beside real data; never in place of it. Our own implementation inspired by thelcars.com's home title area. |
| **Compact toggle grid** | `.lcars-toggle-grid.is-compact` | Two columns of bar-height toggles with name and `ON`/`OFF` on one line. Use in dense control panels. |
| **LCARS data row** | `dl.lcars-data-list` > `.lcars-data-row` > `dt` + `dd` | Read-only key/value readout: 72px peach name block, ice value that truncates with an ellipsis. Add `dd.is-caps` to show a proper name in uppercase (Room). Use it for live facts such as Vision Control's Scene group (Room, People, Objects); never for something the user can change. |
| **LCARS select** | `label.lcars-select` > `span.lcars-select-name` + `select` | Lilac name block beside a native select, one bar high (Home media player: Station, Show, Episode). Group with `.lcars-select-group`. |
| **Computer comm bar** | `div.lcars-comm#lcars-comm` > `.lcars-comm-key` + `.lcars-comm-text` (created by `computerSays()` in `js/core/eggs.js`) | Fixed pill-ended bar at the bottom right: peach "Computer" block, then the message in ice on black. `role="status"`, hides itself after 6s, ignores the pointer. Red during red alert. Use it for light, transient "computer" replies; real errors belong in the card's status line or the alert frame. |
| **Alert acknowledge** | `button.lcars-alert-ack` | Alert-hue block with a rounded right end at the far side of the alert frame. See §3.2. |
| **LCARS slider** | `label.lcars-slider` > name `span` + `input[type=range]` + `output` | Peach name block, ice-accent range, ice value readout. Follows the live value while idle; sends only the latest value while dragging. |

### 3.1 Sweep card (live video and other hero panels)

The standard card frame (top-left elbow, top bar, left spine) closed by a
**bottom elbow** into a thick bottom bar. Controls that act on the whole panel
sit in that bar. Home's camera card ("Vision") uses it, with a Left Eye / Right Eye source
switch (camera 1 / camera 2; VERA's cameras are its eyes, so the UI names them that way).

```html
<section aria-labelledby="…-title" class="card lcars-sweep-card" id="…">
  <span aria-hidden="true" class="frame-break frame-break-spine"></span>
  <span aria-hidden="true" class="frame-break frame-break-lower"></span>
  <a class="lcars-sweep-title" href="#/…" id="…-title">Vision</a>
  <div aria-label="…" class="lcars-sweep-bar lcars-sweep-bar-top" role="group">
    <span aria-hidden="true" class="sweep-block"></span>
    <div class="sweep-readout"><span class="sweep-readout-key">Room</span><span class="sweep-readout-value">Office</span></div>
  </div>
  … content (no h2, no caption under the picture) …
  <div class="lcars-sweep-bar">
    <span aria-hidden="true" class="sweep-block"></span>
    <div aria-label="…" class="cam-switch" role="group">
      <button aria-pressed="true" type="button">Left Eye</button>
      <button aria-pressed="false" type="button">Right Eye</button>
    </div>
  </div>
</section>
```

- **Bottom elbow:** `border-bottom: var(--lcars-bar-height)` in the panel hue,
  with `border-bottom-left-radius: 53px 50px`. The browser draws the inner curve
  from the unequal border widths, matching the top elbow.
- **Top bar:** by default the top border is as thick as the bottom bar
  (`--sweep-top` = `--sweep-bar`) with a matching `53px 50px` elbow, so it can
  carry text. A card can slim it to just fit its text: Home's Vision card sets
  `--sweep-top: 22px`. The title's font follows the bar (`--sweep-top − 2px`),
  and readouts in a top bar sit 2px off its bottom edge.
- **Fluid 16:9 video:** the frame hugs the picture instead of letterboxing it.
  The feed box is `width: 100%; aspect-ratio: 16 / 9`, so its height follows
  the card width. On Home the Vision card takes half the workspace (the area
  right of the menu rail) and the mirrored Vision Control card takes the other
  half at the same height. Vision Control holds the eye switch, a Scene group
  (Room and Objects in view as data rows), the Behavior toggles (including
  Confidence %, which shows or hides the confidence in the overlays) and the
  head-position sliders.
- **Side-by-side breakpoint:** `main` is a size container
  (`container: workspace / inline-size`). Home sits two columns wide only when
  `@container workspace (min-width: 840px)`; below that it stacks in source
  order. Rationale: the video is the card width minus 46px (spine 29 + border
  1 + padding 16), the grid gap is 24px, so 2 × (360 + 46) + 24 ≈ 840px keeps
  the feed at least 360px wide. With the 208px rail that is a ~1085px viewport.
- **Title:** `.lcars-sweep-title` sits at the left of the top bar, past the
  elbow: a black cut-out the height of the bar, uppercase text in the panel
  hue. It is the only title (no `h2`) and links to the related workspace.
  Keep video and other hero content clean: no caption text inside the frame.
- **Top readouts:** `.lcars-sweep-bar-top` mirrors the bottom bar at the top
  right: a lilac `.sweep-block`, then read-only `.sweep-readout` blocks (peach,
  then lilac). Each shows a small key and an uppercase value at the
  bottom-right, truncated with an ellipsis; set `title` to the full text. They
  are plain spans, never buttons: no hover ring and no focus stop.
- **Frame breaks and colour changes:** these are the LCARS habit of chopping a
  bar into differently coloured segments separated by black gaps.
  - The top bar's lilac `.sweep-block` and readouts, split by black gaps.
  - `.frame-break-spine`: a lilac segment across the spine, just below the
    middle.
  - `.frame-break-lower`: the rest of the spine below it turns peach, then a
    black gap hands back to the panel hue just above the bottom elbow.

  The frame breaks are decorative, `aria-hidden`, and `pointer-events: none`.
- **Jewel variety:** the spine breaks and bar blocks ("jewels") are driven by
  card-level variables: `--jewel-top`, `--jewel-height`, `--jewel`,
  `--jewel-lower` and `--jewel-block`. Cards that sit side by side must not
  repeat the same jewel set. Add `.is-alt-jewels` to the companion card: a
  short ice break high on the spine (22%, 40px), a lilac run below it, and
  peach bar blocks. Home's Vision keeps the default set, and Vision Control
  uses the alternate.
- **Bottom sweep bar:** right-aligned on the bottom bar.
  - It holds an optional `.sweep-block` colour chip, then a segmented control.
    Each piece is separated by a `--lcars-gap` black gap. The bar ends square,
    flush with the card's right edge (no rounded end).
  - Active segment: `--lcars-ice` with the rail buttons' white end highlight
    (`inset -6px 0 #fff`). Inactive: `--lcars-dim`. State is carried by
    `aria-pressed`.
- **Knobs:** `--sweep-spine`, `--sweep-top` and `--sweep-bar` on
  `.lcars-sweep-card`. The phone media query reduces them to match the
  narrower card frame.
- **Behaviour:** use the sweep card for one hero panel per page. Don't put it
  on every card, or it stops being special.

### 3.2 Alert frame

A full-width frame directly under the page title: a horizontal bar across the
top that sweeps down into a spine on **both** the left and right, open at the
bottom. Home uses it for alert messages.

```html
<section aria-label="Alerts" class="lcars-alert-frame" data-level="warn" hidden id="…">
  <span aria-hidden="true" class="lcars-alert-title">Alerts</span>
  <div class="lcars-alert-body">
    <div aria-live="polite" class="home-alerts">…pills…</div>
    <button class="lcars-alert-ack" type="button">Acknowledge</button>
  </div>
</section>
```

- **Only shown when there's a message.** JS toggles `hidden`; with nothing to
  report there is no frame and no empty row (the page grid only adds the
  `alerts` area while `#tab-home.has-alerts` is set).
- **Hue = worst alert:** `data-level="fail"` → `--red`, `warn` →
  `--lcars-peach`, otherwise `--lcars-ice`.
- **Title cut-out:** `.lcars-alert-title` sits in the top bar as a black box
  with text in the frame hue.
- Messages are `.pill`s in a wrapping row; ones with a destination are buttons.
- **Acknowledge** hides the current alerts, and the frame with them. Each
  alert has a stable `id`; the acknowledgement is stored per browser
  (`localStorage` `vera-alert-acks`) as `id|level`. An alert that clears and
  comes back, escalates (warn → fail), or is new shows the frame again.
- System health isn't repeated here: it lives in the header's segmented bar
  (`OK` / `DEGRADED`, coloured green / red).

### 3.3 Units

Every measurement shown to the user follows the device-wide unit system
chosen in Settings → Display Units (US Customary, Metric SI/MKS, UK Imperial,
Scientific SI with kelvin). Readings stay in native units on the bus and the
API; convert at render time with `js/core/units.js` (`formatTemp`,
`formatValue`, `convertDisplay`) and re-render on the `vera:units` event.
Never hard-code `°C` / `°F` or a flow/pressure unit in new UI.
Unit symbols are case-sensitive (`pCi/L`, `kPa`, `mL`): never put them in an
element with `text-transform: uppercase`.

### States

- **Hover:** a 2px black inset ring (`box-shadow: inset 0 0 0 2px #000`). Don't change the hue.
- **Active / selected:** a white inset bar on the edge that faces the content, plus a brighter hue.
- **Pressed / lit:** switch to the lit hue (`--lcars-ice`) and show a text state.
- **Disabled / busy:** `opacity: .55` and `cursor: progress`.
- **Focus:** `outline: 3px solid var(--text)` with `outline-offset: 2–3px` on `:focus-visible`. Never remove the focus outline.

## 4. Layout and flow

1. **One question per card.** Each card answers a single question: what's my
   status, what's playing, what does the camera see.
2. **Read top-down by urgency.** Lay out sections in this order:
   - health in the header bar; an alert frame under the page title, only
     while there are messages;
   - the primary visual (camera, with room and people in its top bar) next to
     the most-used controls;
   - the media player at full width under that pair (Pandora and podcasts in
     one bar-title card, with a source selector; Mute sits beside the volume
     slider);
   - secondary actions;
   - Advanced and Engineering data at full width at the bottom.
3. **Name the layout with grid areas.** Use `grid-template-areas` so the
   reading order is explicit. Below the breakpoint (Home: an 840px workspace
   container query), collapse to one column in the same
   order as the source.
4. **Match heights within a row.** Use `align-items: stretch` so cards in a
   row line up. Pair a hero visual with a companion control card of equal
   width (Home: Vision | Vision Control); the visual sets the row height and
   the control card stretches to match. When the control card is the taller
   one, the visual centres vertically in its frame.
5. **Gate by mode, not by duplicate pages.** Mark advanced content with
   `data-min-mode="advanced"` and engineering content with
   `data-min-mode="engineering"`. Normal mode shows only everyday controls.
6. **Navigate with links, not buttons.** Use plain `#/slug` anchors, which the
   hash router handles. Don't add click handlers just to navigate.
7. **Phones (≤700px):** shrink bars (`--lcars-bar-height: 28px`), let the
   header wrap, and shrink the alert frame and sweep readouts.

### 4.1 Easter eggs

Star Trek easter eggs live in `js/core/eggs.js`. Every trigger is listed in
[EASTER_EGGS.md](EASTER_EGGS.md); update it when adding or changing an egg. They stay **cosmetic**: no API
calls, no speech, no head movement and no setting changes. Typed triggers are
ignored while focus is in a form field. Every effect respects
`prefers-reduced-motion` (warp is skipped; red alert shows a static frame).
Route eggs (`#/holodeck` …) must never use the slug of a real page; a test
enforces this. Add new eggs there, reply through `computerSays()`, and keep
quotes to a few words. Effects are body classes that `bodyEffect()` toggles (`is-red-alert`,
`is-warp`, `is-beaming`, `is-shields`, `is-cloaked`, `is-torpedo`). Overlays
such as `.lcars-tribble` use `pointer-events: none` and VERA's palette
tokens. Typed phrases must not contain one another; a test enforces this.

## 5. Accessibility

- Text contrast must be at least 4.5:1. Black on any VERA fill passes; dim text
  on `--bg3` is the lower limit.
- State is never shown by color alone. Toggles show ON/OFF, the health block
  shows OK/Degraded, and pills show words.
- Controls stay real `<button>`, `<input>`, or `<a>` elements with labels.
  Segmented groups use `role="group"` and `aria-label`.
- Decorative codes and icons are `aria-hidden="true"`.

## 6. Do and don't

| Do | Don't |
|---|---|
| Separate blocks with 6px black gaps | Draw 1px borders around LCARS blocks |
| Round only the outer ends of a run | Round every segment into separate pills |
| Put black condensed labels at the bottom-right | Center light text on light fills |
| Use tokens and the workspace hue | Add one-off hex colors |
| Put a cross-link in the card title | Add "Open X" buttons to the card body |
| Show ON/OFF beside the lit color | Rely on color alone |

## 7. Checklist for new UI

- [ ] Reused a component from §3, or added a new one to §3.
- [ ] Used only tokens from §2.
- [ ] Flat fills, black gaps, no outline borders on LCARS blocks.
- [ ] Labels are uppercase, condensed, black on color, bottom-right.
- [ ] Hover, active, focus, and disabled states follow §3.
- [ ] Reading order follows §4; the one-column fallback has been checked below the layout breakpoint and at ≤700px.
- [ ] Advanced and Engineering content is gated with `data-min-mode`.
- [ ] State is readable without color; decorative codes are `aria-hidden`.
- [ ] Hero / live-video panels use the §3.1 sweep card; frame breaks are decorative and `aria-hidden`.
- [ ] Bumped the `?v=` cache keys in `index.html` (and `js/main.js` imports).

## 8. Sources

- Wikipedia, "LCARS": <https://en.wikipedia.org/wiki/LCARS>. History,
  Okuda's design intent, and terminology.
- Rick Sternbach & Michael Okuda, *Star Trek: The Next Generation Technical
  Manual* (Pocket Books, 1991). The original description of the in-universe
  interface.
- *The LCARS Website*: <https://www.thelcars.com>. A fan reference showing the
  LCARS conventions applied to HTML/CSS. Its templates are covered by their own
  licence (personal, non-commercial use with attribution; no redistribution)
  and are **not** included in this repository.
