---
description: "Use when building or modifying VERA web UI (HTML, CSS, JS under src/web). Enforces the LCARS design guide."
applyTo: "src/web/**"
---
# LCARS UI rules

Before you add or change any web UI element, read
[docs/ui/LCARS_DESIGN_GUIDE.md](../../docs/ui/LCARS_DESIGN_GUIDE.md) and follow it:

- Reuse the components in §3 (frame, rail button, segmented bar, segmented
  selector, lit/unlit toggle,
  readout, alert frame, sweep title, sweep readout, status pill, card cross-link, sweep
  card). Live video and hero panels use the §3.1 sweep card with a bottom elbow
  and frame breaks; page alerts use the §3.2 alert frame, shown only while
  there are messages. Active/selected buttons use the
  white end highlight (`inset -6px 0 #fff`), never an underline.
  Don't invent near-duplicates.
- Use only the `:root` tokens in `src/web/static/style.css`. Use flat fills and
  6px black gaps; never put outline borders on LCARS blocks.
- Labels are uppercase and condensed, black on color, and sit bottom-right.
  State is never shown by color alone.
- Order cards by urgency with named grid areas. Gate advanced and engineering
  content with `data-min-mode`.
- Show measurements in the user's chosen unit system via `js/core/units.js`
  (§3.3); never hard-code `°C`/`°F` or other units.
- When you introduce a new component or token, update the guide in the same commit.
- Run through the guide's §7 checklist before you commit.
