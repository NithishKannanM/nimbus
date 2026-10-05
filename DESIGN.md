---
version: alpha
name: Nimbus-dashboard
description: "A plotter-paper operations view. Light sage-tinted stock (#EEF0EA) printed with graph-paper ruling, drawn on with a four-pen ink set. Three inks are claims about a worker (DECLARED graphite, RESERVED blue ballpoint, OBSERVED red); red also marks anything that diverged from expectation. Flat, square, shadowless. Three type voices: Instrument Sans legend plates, Martian Mono readouts, IBM Plex Mono logbook."

colors:
  paper: "#EEF0EA"
  rule: "#D5D8CE"
  ink: "#1C1F1A"
  declared: "#5E645C"
  reserved: "#1B4D8F"
  observed: "#C2410C"

typography:
  legend:   { fontFamily: Instrument Sans, fontSize: 11px, fontWeight: 600, letterSpacing: 0.14em, textTransform: uppercase }
  heading-1: { fontFamily: Instrument Sans, fontSize: 13px, fontWeight: 700, letterSpacing: 0.14em, textTransform: uppercase }
  heading-2: { fontFamily: Instrument Sans, fontSize: 11px, fontWeight: 700, letterSpacing: 0.14em, textTransform: uppercase }
  finding:  { fontFamily: IBM Plex Mono, fontSize: 13px, lineHeight: relaxed, maxWidth: 62ch }
  log-body: { fontFamily: IBM Plex Mono, fontSize: 11px }
  log-id:   { fontFamily: IBM Plex Mono, fontSize: 12-13px }
  readout:  { fontFamily: Martian Mono, fontSize: 15px }
  svg-readout: { fontFamily: Martian Mono, fontSize: 13px/700 observed, 11px reserved }
  footnote: { fontFamily: IBM Plex Mono, fontSize: 11px }

rounded: { all: 0px }
spacing: { page-x: 24px, page-y: 28px, section-gap: 40px, hero-gap: 32px, panel-pad: 16px, grid-gap: 24px }
---

# Nimbus dashboard design system

This file is the contract for `frontend/src`. Tokens live in `frontend/src/lib/theme.ts` (INK, WINDOW_S, LANE_H) and are mirrored as literals in `frontend/tailwind.config.js`; change both together. Rationale and behaviour notes live in `frontend/README.md`.

## Overview

A bound lab record, not a devtool. The page makes one argument: a worker has three separate descriptions (DECLARED by the operator, RESERVED by Nimbus's bookkeeping, OBSERVED by psutil on the host), they are allowed to disagree, and only one placement policy may read the third. The palette therefore carries meaning, not status. The signature element is the **divergence band**: per worker, reserved (blue) against observed (red), the area between them shaded and split by sign, with one derived sentence under it (`Finding.tsx`).

The user rejected a dark-slate devtool look as "generic AI". Light stock, ruling, ink and mono type are the identity. Everything is flat and square.

## Colors

| Token | Hex | Role | Contrast on paper |
|---|---|---|---|
| paper | #EEF0EA | ground, sage-tinted, not cream | n/a |
| rule | #D5D8CE | graph ruling (body::before, 40px + 8px grids, opacity 0.34) | decorative |
| ink | #1C1F1A | primary text, strokes, active control fill | 14.5:1 |
| declared | #5E645C | graphite: operator config, inert, labels, secondary text | 5.3:1 |
| reserved | #1B4D8F | blue ballpoint: Nimbus bookkeeping, normal in-flight states | 7.3:1 |
| observed | #C2410C | red pen: the reading contradicts the register | 4.5:1 (bold/legend or 11px+ only) |

Semantics:
- DECLARED = graphite, RESERVED = blue, OBSERVED = red. Never merge them into one utilisation figure.
- Red also means "diverged from expectation": retried, failed, timed out, dead-lettered, no candidate, tie-break, truncated, low load, stale poll ("api unreachable"), unhealthy workers. A zero friction counter is ink, not red.
- Neutral tints are ink at opacity (`ink/10`, `/15`, `/20`, `/30`), never new greys.
- Band fills: observed above reserved = red @ 0.18, reserved above observed = blue @ 0.12.
- No green, no success/error pair, no fifth colour, no dark ground, no gradients. (declared was #6B7169 at 4.36:1; darkened to #5E645C to pass AA as text.)
- Never hard-code a hex in a component. Use Tailwind token classes, or `INK.*` in SVG/inline styles.

## Typography

- **Instrument Sans** (`font-legend`): legend plates and labels. Uppercase, 0.14em tracking, 600 weight, 11px (`.legend` class). h1 13px/700, section h2 11px/700.
- **Martian Mono** (`font-meter`): readouts only, the number you look at. Queue stats 15px; strip readouts 13px (observed, bold) over 11px (reserved).
- **IBM Plex Mono** (`font-log`): logbook, tables, traces, the Finding sentence. Body 11px, ids 12-13px, finding 13px relaxed, `max-w-[62ch]`.
- `font-variant-numeric: tabular-nums` on body. Use `.stat` on right-aligned numbers.
- Floor is 11px for HTML and 9px for SVG axis text. Do not add 10px or smaller. No Inter, no serif.
- Weights available: 400, 500, 600 (`font-600`), 700 (`font-700`).

## Layout

- Single column, `max-w-[1180px]`, `px-6 py-7`, centred. Order: header rail + strategy switch, divergence figure + Finding, Fleet, Decision ledger, Queue and Jobs (2-col at `lg`, gap-6), Archived runs, footer.
- Section rhythm: `mt-8` after the header, `mt-10` between sections. Panels use `p-4`.
- Dividers are `border-ink/15..20` hairlines or `gap-px bg-ink/15` seams (Fleet grid). Prefer hairlines over boxes; `.plate` (border `ink/15`, `bg-paper/80`) is the only boxed container.
- Fleet: `sm:grid-cols-2 lg:grid-cols-3`; each plate rows are `grid-cols-[64px_1fr_auto]`.
- Divergence SVG: viewBox 1080 wide, PAD_L 104, PAD_R 78, LANE_H 66 with a 20px GUTTER, window 90 s, newest sample pinned to the right edge. Fixed time axis, never rescaled to data.

## Elevation & Depth

None. No shadows, no blur stacks, no glass. Depth is hairlines, the ruled ground, and ink opacity. `.plate` carries a 1px backdrop blur only so ruling does not fight table text. Do not add drop shadows.

## Shapes

Radius 0 everywhere. Square lamps (8px), square buttons, square badges. Pen heads are a 2.6px circle plus a triangular nib; the nib marker is reused in the decision ledger to mark the selected worker.

## Components

- **HeaderRail**: h1, strategy written as `strategy = ordering + placement · version`, two Lamps (filled = true, hollow = false; red fill because both concern observed telemetry), stale badge (red outline, `role=status`).
- **StrategySwitch**: the only write (`PUT /scheduler`). Square buttons, `min-h-[32px]`, active = `bg-ink text-paper`, others `border-ink/30`; `aria-pressed`, group labelled, 1px press translate, error in `role=status`. Focus ring is the global 2px reserved outline.
- **DivergenceStrip**: SVG `role=img` with aria-label; scroll container (`min-width: 720px`) so text stays legible at 375px; pens draw in once (`.pen`, 1100ms), band fades in (`.band`).
- **Finding**: one derived sentence; three branches (telemetry off, waiting, agree below 20 points, diverged). Never hand-written.
- **FleetPlates**: three rows per worker, declared (no bar), reserved bar, observed bar (3px, `role=meter`), heartbeat footer.
- **DecisionTrace**: expandable ledger rows (`aria-expanded`), candidate table always shows the observed column even when null.
- **QueueLedger / JobsLog / BenchmarkTable**: `.plate` panels, tables with `scope=col` headers and sr-only captions, horizontal scroll on narrow screens. Friction counters are red only when non-zero.
- **Panel states**: loading text "reading the X…", empty text states what is empty, failure appends red "api unreachable · last known" while keeping stale data. A stale panel is labelled, never blanked.

## Do's and Don'ts

Do:
- Use red for one meaning: the reading contradicts the record.
- Keep stale data on screen when a poll fails and label it.
- Derive captions from the same data the figure draws.
- Give every polled panel loading, empty and failure text.
- Add `aria-label`/caption to every new SVG or table.

Don't:
- Add green/red success-error colours, a fifth ink, gradients, shadows, rounded corners or a dark theme.
- Add a chart library. All charts are hand-rolled SVG.
- Add any write beyond `PUT /scheduler`.
- Blend declared/reserved/observed into one bar.
- Put colour on meaning-free decoration; do not use dots, pills or badges for ornament.
- Hard-code hex values or font sizes below 11px (9px SVG axis labels excepted).

## Responsive Behavior

- 375px: single column; header rail, strategy buttons and legend wrap; Fleet one column; Queue and Jobs stack; tables sit in `overflow-x-auto` wrappers; the divergence strip scrolls horizontally at 720px minimum rather than shrinking text to unreadable size.
- `sm` 640px: Fleet two columns. `lg` 1024px: Fleet three columns, Queue and Jobs side by side.
- 1440px: content stays at 1180px, ground ruling fills the viewport.
- Use `min-h-[100dvh]`, not `h-screen`.
- Touch targets: strategy buttons and ledger rows are at least 32px and 40px tall.

## Iteration Guide

1. Change a colour only in `theme.ts` and `tailwind.config.js` together, then re-check contrast (text needs 4.5:1 on #EEF0EA).
2. New panel: `.plate p-4`, `.legend` h2, `usePoll`, and loading/empty/error text from the Panel states rule.
3. New number: `font-meter` if it is a headline readout, `font-log stat` if tabular.
4. New divergence from expectation: use `text-observed`, same size as the happy path.
5. Motion: only data and the one load-in draw. Gate anything new behind `motion-reduce`; `index.css` already collapses transitions and the pen animation under `prefers-reduced-motion`.
6. Run `cd frontend && npm run build` and `npm run typecheck`.

## Known Gaps

- observed #C2410C is 4.51:1, only just AA; do not lighten it, and avoid it below 11px non-bold.
- No skeleton loaders; first load is a single "connecting" line. Per-panel loading is text only.
- The strip's reading is visual; screen readers get the aria-label and the Finding sentence but not per-sample data.
- Fonts load from Google Fonts at runtime (no self-hosting, no offline fallback beyond system mono/sans).
- Header "—" placeholders for empty cells and em dashes in a few sentences remain; acceptable here, as the page is an instrument, not marketing copy.
- No Playwright/visual regression. Narrow (~485px) and 936px widths were checked by hand in Chrome against a live server on 2026-10-05; there is no automated check.
- Below 720px the strip scrolls and opens at its live (right) edge, so the worker labels on its left are off-screen until the reader scrolls back. Fleet plates below carry the same worker ids.
- `ink/8` style opacity values are not used; stay on 10/15/20/30.
