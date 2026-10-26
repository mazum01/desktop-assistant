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
| **Segmented button bar** | `.mode-toggle` + `header .badge` | Square blocks split by gaps; only the outer ends are rounded. Active segment: peach with a white inset underline (`inset 0 -6px #fff`). Use `aria-pressed`. |
| **Lit/unlit toggle** | `.lcars-toggle` > `input` + `.lcars-toggle-face` | Block with the right end rounded. Unlit = `--lcars-dim`, lit = `--lcars-ice`. Always shows an `ON`/`OFF` readout. The native checkbox stays in the DOM, visually hidden but focusable. |
| **Readout** | `.stat-box` > `.stat-label` + `.stat-value` | Small uppercase label above a large value on black. |
| **Status pill** | `.pill.pill-ok/-warn/-bad` | Short status words. Never color alone: the text must state the condition. |
| **Card cross-link** | `h2 > a.card-link` | Floats right in the card title, ice-colored, ends with `›`. Use it instead of an "Open X" button. |
| **Action button** | `.btn` | Existing rounded action buttons inside cards. Use them for verbs (Play, Say), not for state. |
| **Decorative code** | `.lcars-code` | `aria-hidden`, sparse (3–6 on the page). |

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
   - a status strip (health, alerts) at full width;
   - the primary visual (camera) next to the most-used controls;
   - secondary actions;
   - Advanced and Engineering data at full width at the bottom.
3. **Name the layout with grid areas.** Use `grid-template-areas` so the
   reading order is explicit. Below 1150px, collapse to one column in the same
   order as the source.
4. **Match heights within a row.** Use `align-items: stretch` and let the
   primary visual fill its column (`flex: 1`), so cards don't leave ragged gaps.
5. **Gate by mode, not by duplicate pages.** Mark advanced content with
   `data-min-mode="advanced"` and engineering content with
   `data-min-mode="engineering"`. Normal mode shows only everyday controls.
6. **Navigate with links, not buttons.** Use plain `#/slug` anchors, which the
   hash router handles. Don't add click handlers just to navigate.
7. **Phones (≤700px):** shrink bars (`--lcars-bar-height: 28px`), let the
   header wrap, and use two-column readout strips.

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
- [ ] Reading order follows §4; the one-column fallback has been checked at ≤1150px and ≤700px.
- [ ] Advanced and Engineering content is gated with `data-min-mode`.
- [ ] State is readable without color; decorative codes are `aria-hidden`.
- [ ] Bumped the `?v=` cache keys in `index.html` (and `js/main.js` imports).

## 8. Sources

- Wikipedia, "LCARS": <https://en.wikipedia.org/wiki/LCARS>. History,
  Okuda's design intent, and terminology.
- Rick Sternbach & Michael Okuda, *Star Trek: The Next Generation Technical
  Manual* (Pocket Books, 1991). The original description of the in-universe
  interface.
- *The LCARS Website*: <https://www.thelcars.com>. A fan reference showing the
  LCARS conventions applied to HTML/CSS. Its templates are covered by their own
  licence and are **not** included in this repository.
