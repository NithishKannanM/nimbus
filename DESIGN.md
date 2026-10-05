---
version: alpha
name: Nimbus-dashboard
description: "An industrial brutalist operations view, Swiss industrial print mode. Matte unbleached paper (#F2F0EB), carbon ink (#0A0A0A), one hazard accent (#F54F00). Three descriptions of a worker stay visually distinct: DECLARED grey hatch, RESERVED solid carbon, OBSERVED hazard. Hazard also marks anything that diverged from expectation. Flat, square, shadowless, gradient-free. Archivo Black macro type over IBM Plex Mono data."

colors:
  paper: "#F2F0EB"
  ink: "#0A0A0A"
  rule: "#0A0A0A"
  hairline: "#C9C5BB"
  declared: "#5F5C56"
  reserved: "#0A0A0A"
  observed: "#F54F00"
  observedText: "#B33A00"

typography:
  masthead: { fontFamily: Archivo Black, fontSize: "clamp(3.5rem, 11vw, 9.5rem)", letterSpacing: -0.04em, lineHeight: 0.9, textTransform: uppercase }
  section-title: { fontFamily: Archivo Black, fontSize: "clamp(1.6rem, 3.6vw, 2.75rem)", letterSpacing: -0.04em, lineHeight: 0.9, textTransform: uppercase }
  big-numeral: { fontFamily: Archivo Black, fontSize: "48px fleet / 30px queue / 28px strip" }
  label: { fontFamily: IBM Plex Mono, fontSize: 11px, fontWeight: 500, letterSpacing: 0.08em, textTransform: uppercase }
  data: { fontFamily: IBM Plex Mono, fontSize: 12-13px, fontVariantNumeric: tabular-nums }
  finding: { fontFamily: IBM Plex Mono, fontSize: 15-16px, lineHeight: relaxed, maxWidth: 62ch, textTransform: none }

rounded: { all: 0px }
spacing: { page-x: 16px/24px, page-top: 20px, section-gap: 40px, section-pad-top: 20px, grid-gap: 32px }
---

# Nimbus dashboard design system

This file is the contract for `frontend/src`. Tokens live in `frontend/src/lib/theme.ts` (INK, WINDOW_S, LANE_H, and the SVG-pattern helpers) and are mirrored as literals in `frontend/tailwind.config.js`; change both together. No hex appears anywhere else in `src`. Rationale and behaviour notes live in `frontend/README.md`.

## Overview

On 2026-10-05 the user replaced the earlier "plotter paper" direction with **industrial brutalist, Swiss industrial print (light) mode**. The ink semantics carried over unchanged: graphite became the declared grey hatch, blue ballpoint became carbon black, and red became hazard orange. Only the surface changed: sage graph paper and soft hairlines are now matte stock, solid 2px black rules, a giant masthead and dense mono tables. The dark CRT mode is not used and must not be mixed in.

The page still makes one argument: a worker has three separate descriptions (DECLARED by the operator, RESERVED by Nimbus's bookkeeping, OBSERVED by psutil on the host), they are allowed to disagree, and only one placement policy may read the third. The palette therefore carries meaning, not status, and the three descriptions differ in treatment as well as colour so they survive greyscale. The signature element is the **divergence strip**: per worker, reserved (solid black line) against observed (hazard line), the area between them hatched and split by sign, with one derived sentence under it (`Finding.tsx`).

Decoration (`[ ]`, `///`, `+` crosshairs, index numerals 01..07) is structural only. Every value on screen comes from the API or is a real constant (worker ids, strategy, policy version, the 90 s window). No invented revision strings.

## Colors

| Token | Hex | Role | Contrast on paper |
|---|---|---|---|
| paper | #F2F0EB | matte substrate | n/a |
| ink | #0A0A0A | text, all structural rules (1px/2px solid), active control fill | 17.4:1 |
| rule | #0A0A0A | alias of ink for structural lines | 17.4:1 |
| hairline | #C9C5BB | light row rules inside tables; never text | 1.5:1 (decorative) |
| declared | #5F5C56 | operator config: outline or hatch, labels, secondary text; never a solid fill | 5.9:1 |
| reserved | #0A0A0A | Nimbus bookkeeping, solid | 17.4:1 |
| observed | #F54F00 | hazard: marks, fills, strokes, bars, hatch, and type >=24px bold | 3.07:1 (graphics and large type, WCAG 3:1) |
| observedText | #B33A00 | the accent for small text | 5.2:1 (body text, 4.5:1) |

Also: paper on ink 17.4:1 (active buttons), ink on hazard about 5.7:1.

The brief's hazard #FF4F00 measured 2.89:1, below the 3:1 needed for graphics, so it was shifted to #F54F00 (3.07:1), the smallest change that passes. observedText #B33A00 passed unchanged.

Semantics:
- DECLARED = grey hatch/outline, RESERVED = solid carbon, OBSERVED = hazard. Never merge them into one utilisation figure or score.
- Hazard also means "diverged from expectation": retried, failed, timed out, dead-lettered, no candidate, tie-break, truncated, low load, stale poll, api unreachable, unhealthy workers. A zero friction counter is ink, not hazard.
- Exactly one accent. No green, no red/green status pair, no second accent, no dark ground.
- Job statuses: RUNNING = ink bold, COMPLETED = plain ink, divergent states = observedText.
- Never hard-code a hex outside `theme.ts`. Use token classes, or `INK.*` in SVG and inline styles.

## Typography

- **Archivo Black** (`.display`, `font-display`): macro type. Uppercase, letter-spacing -0.04em, line-height 0.9. It ships one weight: never add `font-bold`. Masthead `clamp(3.5rem, 11vw, 9.5rem)`; numbered section titles `clamp(1.6rem, 3.6vw, 2.75rem)` as "01" (grey) plus the name; big numerals: observed % in fleet plates (48px), queue counters (30px), strip readouts (28px). Hazard is only allowed on type this large.
- **IBM Plex Mono** (`font-mono`): everything else. `.label` is 11px, 500, uppercase, tracking 0.08em, for labels and metadata; data cells 12-13px with tabular-nums. The Finding sentence is sentence-case Plex Mono, 15-16px, with a 6px left rule: hazard when it reports a divergence, black otherwise.
- Small accent text is `text-observedText`, never `text-observed`.
- Floor is 11px for HTML and 10px for SVG axis text.

## Layout

- Single column, `max-w-[1280px]`, `px-4 sm:px-6`. The masthead has NIMBUS left and a bracketed metadata cluster right (strategy formula, policy version, workers, telemetry lamp, placement lamp), then a 2px black rule, then the strategy switch.
- Sections 01 The divergence, 02 Fleet, 03 Decision ledger, 04 Queue, 05 Jobs, 06 Archived runs, 07 Notes. Each is separated by a full-width 2px black rule (`border-t-2 border-ink pt-5`, `mt-10`).
- Fleet uses solid borders, not a `gap-px` black parent: with 3 workers on a 2-column row a black gap would show a black hole in the empty cell. Other compartments use solid borders too.
- Queue and Jobs: `grid-cols-1 lg:grid-cols-2`, `gap-x-8 gap-y-10`.
- Divergence SVG: viewBox 1080 wide, PAD_L 104, PAD_R 104, LANE_H 84 with a 20px GUTTER, window 90 s, newest sample pinned to the right edge. Fixed time axis, never rescaled to data. The figure scrolls horizontally below 720px and opens at its live right edge.
- Jobs log scroll region keeps `scrollbar-gutter: stable`.
- Warning stripe: a 16px band of repeating 45 degree hazard stripes under the masthead, shown only while `isDiverged(lanes, telemetryOn)` is true, the same function the Finding reads.
- Paper grain: one fixed SVG-noise layer, opacity 0.05, `pointer-events: none`, hidden under `prefers-reduced-transparency`.

## Elevation & Depth

None. No shadows, blur, gradients or translucency. Depth is solid black rules. The only layered effect is the grain overlay at 5% opacity. Hatching and stripes are SVG patterns (hard-edged), not CSS gradients.

## Shapes

Radius 0 everywhere; Tailwind `borderRadius` is zeroed globally. Square lamps, square buttons, square pen-head marks (the same square marks the selected worker in the decision ledger). `+` crosshairs sit at strip axis intersections.

## Components

- **Masthead (HeaderRail)**: h1 NIMBUS, `[ STRATEGY fifo = ordering + placement ]`, `[ POLICY VERSION ]`, `[ WORKERS n ]`, two square lamps (filled hazard = true, hollow grey = false), stale badge (hazard outline, `role=status`), conditional warning stripe.
- **StrategySwitch**: the only write (`PUT /scheduler`). Square segmented mono buttons, 2px black borders, active = black fill with paper text, hover = 3px hazard underline, global 3px hazard focus-visible outline; `aria-pressed`, labelled group, error in `role=status`.
- **DivergenceStrip**: SVG `role=img`; reserved = solid black 2.5px line, observed = hazard 2.5px line; band between = hatch (hazard 45 degree where observed > reserved, grey -45 degree where reserved > observed); solid black lane separators; `+` marks; mono axis labels; Archivo observed numeral and `RES n%` readout at the right edge. Lines draw in once (`.pen`), band fades in (`.band`).
- **Finding**: one derived sentence; branches unchanged (telemetry off, waiting, agree below 20 points, booked-but-idle, unbooked load). The left rule follows `isDiverged`.
- **FleetPlates**: numbered plates, `[ STATUS ]`, big hazard observed numeral, three rows with distinct treatments (declared = hatched outline track, reserved = solid black fill, observed = hazard fill; the two measured ones carry `role=meter`).
- **DecisionTrace**: expandable ledger rows (`aria-expanded`), candidate table always shows the observed column even when null.
- **QueueLedger / JobsLog / BenchmarkTable**: dense mono tables, uppercase headers over a 2px black rule, hairline row rules, `scope=col` headers, sr-only captions, horizontal scroll on narrow screens. Evidence badges keep their derivation and render as `[ PRIMARY ]`.
- **Panel states**: loading text "reading the X…", empty text states what is empty, failure shows `[ api unreachable · last known ]` while keeping stale data.

## Do's and Don'ts

Do:
- Use hazard for one meaning: the reading contradicts the register.
- Keep declared, reserved and observed three separate treatments.
- Keep stale data on screen when a poll fails and label it.
- Derive captions and the warning stripe from the same data the figure draws.
- Give every polled panel loading, empty and failure text; give every SVG or table a label or caption.

Don't:
- Add a second accent, green/red status colours, gradients, shadows, border-radius, translucency, or the dark CRT mode.
- Use `text-observed` on text smaller than 24px bold.
- Add a chart library or any write beyond `PUT /scheduler`.
- Fill declared with a solid colour or blend the three descriptions into one bar.
- Invent data: no fake revision strings or counters.
- Add `font-bold` to Archivo Black.

## Responsive Behavior

- 390px: single column; the masthead scales through `clamp`, the metadata cluster stacks under it, strategy buttons wrap, Fleet is one column, Queue and Jobs stack (`grid-cols-1`), tables sit in `overflow-x-auto` wrappers, and the strip scrolls horizontally at 720px minimum, opening at its live right edge. `body` has `overflow-x: hidden` as a backstop; no page-level horizontal scroll.
- `sm` 640px: Fleet two columns. `lg` 1024px: Fleet three columns, Queue and Jobs side by side.
- Use `min-h-[100dvh]`, not `h-screen`. Touch targets: strategy buttons 36px, ledger rows 40px.

## Iteration Guide

1. Change a colour only in `theme.ts` and `tailwind.config.js` together, then recompute contrast (text 4.5:1, graphics and large type 3:1, on #F2F0EB).
2. New section: `SectionHead` with the next index numeral, a 2px top rule, `usePoll`, and loading/empty/error text.
3. New big number: `.display` at 24px or more. New tabular number: `font-mono` with `stat`.
4. New divergence from expectation: `text-observedText` for text (or `text-observed` if large), same size as the happy path.
5. Motion: only data and the one load-in draw. Gate anything new behind `motion-reduce`.
6. Run `cd frontend && npx tsc --noEmit && npm run build`.

## Known Gaps

- observed #F54F00 is 3.07:1, only just over the graphics threshold; do not lighten it. It is the 24px+ numeral and the strip lines and hatch; small text uses #B33A00.
- Hazard hatch and the grey hatch are told apart by angle and colour; a reader with no colour vision still gets the angle, but the pattern is fine at 6px pitch.
- The Finding's wording refers to "observed trace" and "reserved trace" (formerly "red pen" and "blue trace"), since those colour names no longer apply. Its logic is unchanged.
- No skeleton loaders; first load is a single "connecting" line.
- The strip's reading is visual; screen readers get the aria-label and the Finding sentence but not per-sample data.
- Fonts load from Google Fonts at runtime; fallbacks are Impact and system mono.
- Below 720px the strip opens at its live edge, so the worker labels on its left are off-screen until scrolled back. Fleet plates carry the same ids.
- No automated visual regression; the retheme was verified by build and typecheck only at the time of writing.
