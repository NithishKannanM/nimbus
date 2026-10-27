---
version: beta
name: Nimbus-dashboard
description: "A Modernize-style admin dashboard: white sidebar, sticky status bar, summary tiles, white cards with soft shadow. Three descriptions of a worker stay visually distinct: DECLARED slate hatch, RESERVED solid blue, OBSERVED coral. Coral also marks anything that diverged from expectation. Plus Jakarta Sans UI over IBM Plex Mono IDs."

colors:
  paper: "#FFFFFF"
  canvas: "#FFFFFF"
  ink: "#2A3547"
  hairline: "#E5EAEF"
  muted: "#F2F6FA"
  declared: "#5A6A85"
  reserved: "#5D87FF"
  reservedLight: "#ECF2FF"
  observed: "#E85D37"
  observedText: "#B93D18"
  observedLight: "#FDEDE8"

typography:
  page-title: { fontFamily: Plus Jakarta Sans, fontSize: 24px, fontWeight: 600 }
  card-title: { fontFamily: Plus Jakarta Sans, fontSize: 18px, fontWeight: 600 }
  tile-value: { fontFamily: Plus Jakarta Sans, fontSize: 24px, fontWeight: 600 }
  body: { fontFamily: Plus Jakarta Sans, fontSize: 13-14px }
  eyebrow: { fontFamily: Plus Jakarta Sans, fontSize: 12px, fontWeight: 600, textTransform: uppercase, letterSpacing: 0.04em }
  id: { fontFamily: IBM Plex Mono, fontSize: 12-13px, fontVariantNumeric: tabular-nums }

rounded: { card: 7px, control: 5px, chip: 9999px }
shadow: { card: "0 9px 17.5px rgba(0,0,0,0.05)" }
spacing: { sidebar: 270px, topbar: 70px, card-pad: 30px (24px mobile), grid-gap: 24px, content-max: 1400px }
---

# Nimbus dashboard design system

This file is the contract for `frontend/src`. Colour tokens are CSS variables (RGB channels) in `frontend/src/index.css`, `:root` for light and `.dark` for dark; Tailwind colours read them (`tailwind.config.js`). SVG and inline styles cannot read Tailwind classes, so the same values are mirrored as `LIGHT` and `DARK` in `frontend/src/lib/theme.ts` and read through `useInk()` (`lib/themeMode.tsx`). Change all three together. After editing `tailwind.config.js`, restart the Vite dev server: it does not pick up config changes live. No hex appears anywhere else in `src`. Rationale and behaviour notes live in `frontend/README.md`.

## Overview

On 2026-10-12 the user replaced the industrial brutalist theme (which had replaced plotter paper) because it read as a toy project, and pointed at the Modernize free React MUI dashboard as the reference. The layout copies that template's structure and tokens, built with Tailwind rather than MUI.

The colour semantics carried over unchanged: the declared grey hatch became a slate hatch, carbon became primary blue, and hazard became coral. The page still makes one argument: a worker has three separate descriptions (DECLARED by the operator, RESERVED by Nimbus's bookkeeping, OBSERVED by psutil on the host), they are allowed to disagree, and only one placement policy may read the third. The signature element is still the divergence chart (`DivergenceStrip.tsx`) with its derived Finding sentence.

Every value on screen comes from the API or is a real constant. No avatars, no fake users, no invented counters, even where the template has them.

## Colors

| Token | Hex | Role | Contrast on white |
|---|---|---|---|
| ink | #2A3547 | body text, titles | 12.4:1 |
| declared | #5A6A85 | secondary text; DECLARED as outline or hatch, never a solid fill | 5.5:1 |
| hairline | #E5EAEF | card and row borders; never text | decorative |
| muted | #F2F6FA | bar tracks, neutral chips, segmented-control ground | decorative |
| reserved | #5D87FF | RESERVED, primary actions, active nav and strategy | 3.3:1 (graphics, white text on it at 14px semibold) |
| reservedLight | #ECF2FF | tile icon ground, reserved chips, sidebar status card | decorative |
| observed | #E85D37 | OBSERVED and divergence: lines, bars, dots, type >=24px | 3.5:1 (graphics and large type) |
| observedText | #B93D18 | the accent for small text | 5.6:1 on white, 4.9:1 on observedLight |
| observedLight | #FDEDE8 | divergent chips, diverged Finding background | decorative |

Dark mode (Modernize's dark surfaces), contrast on paper #2A3447:

| Token | Hex | Contrast |
|---|---|---|
| canvas / paper | #202936 / #2A3447 | n/a |
| ink | #EAEFF4 | 10.8:1 |
| declared | #98A4B8 | 5.0:1 |
| hairline / muted | #333F55 / #343F54 | decorative |
| reserved / reservedLight | #5D87FF / #253662 | 3.8:1 graphics |
| observed = observedText | #FA896B | 5.3:1 (4.9:1 on observedLight #4B313D) |

Cards drop the shadow in dark and use a 1px hairline ring instead. Neutral chips use ink text in dark, since declared on muted is only 4.2:1.

Semantics:
- DECLARED = slate hatch/outline, RESERVED = solid blue, OBSERVED = coral. Never merge them into one utilisation figure.
- Coral also means "diverged from expectation": retried, failed, timed out, dead-lettered, no candidate, tie-break, truncated, low load, stale poll, api unreachable, unhealthy workers. A zero friction counter is not coral.
- Exactly one accent. The template's cyan, green and amber are not used. No red/green status pair.
- Job statuses as chips: queued/scheduled/running = reserved chip, completed = neutral chip, divergent = observed chip.
- Small accent text is `text-observedText`, never `text-observed`.

## Layout

- Sidebar (`Sidebar.tsx`): fixed 270px, white, right hairline. Logo, grouped anchor links (Home / Monitor / Evidence) with scroll-spy, active item filled blue with 7px corners. Bottom card shows the running strategy with ordering, placement and version listed underneath (where the template advertises its Pro version); long snake_case names wrap after underscores (`<wbr>`). Under `lg` it becomes an overlay opened from the top bar.
- Top bar (`HeaderRail.tsx`): sticky 70px; menu button under `lg`; right side has the stale chip, two status pills (telemetry collecting, placement reads it), each with a filled or hollow dot so it reads without colour, and the light/dark toggle (moon/sun, `aria-pressed`).
- Theme: `index.html` applies the saved choice (`localStorage["nimbus-theme"]`, read in try/catch) or the system preference before first paint, so there is no flash; the toggle stores the choice.
- Page header: "Dashboard" title, worker count and policy version, strategy segmented control on the right.
- Then, 24px apart: four summary tiles (`StatTiles.tsx`), active algorithm card, divergence card, live strategy comparison, fleet card, decisions (2/3) + queue (1/3), recent jobs, archived runs, footer note.
- Cards (`Card` in `SectionHead.tsx`): white, 7px radius, card shadow, 30px padding, 18px semibold title with a 14px slate subtitle and an optional action on the right.

## Components

- **StatTiles**: icon in a 48px tinted circle, label, 24px value, small footnote. A tile takes the coral treatment only when its condition is divergent (unhealthy workers, the Finding's divergence test, friction events).
- **ActiveAlgorithm**: names the running strategy and version, shows three compact steps (order, filter, place) with the detail and formula behind each one's InfoTip, and a bold blue "Last decision" panel (worker in 26px bold, job, class, score; reason and weights in its InfoTip). Chip: coral "Reads observed telemetry" for adaptive, neutral "Reservations only" otherwise. The prose in `PLACEMENT` restates `scheduler/placement/`; update both together.
- **StrategyComparison**: polls `GET /scheduler/comparison` (`backend/services/comparison.py`). One row per strategy used since the API started, with each job counted under the strategy of its final placement decision. Columns: active time, share sent to a worker at >=80% observed CPU (coral bar), completed, per minute, mean exec time (blue bar, widest = slowest), p95. The card's InfoTip says it is live and uncontrolled, and that the benchmark archive is the controlled comparison.
- **InfoTip** (`InfoTip.tsx`): an 18px circled "i" that shows an explanation on hover, focus or tap, portalled to `<body>` so tables cannot clip it; Escape or scroll closes it. The user asked for less text on the page (2026-10-12), so cards keep a subtitle of a few words and put anything longer behind `Card`'s `info` prop or an inline InfoTip. Do not put any explanation longer than one line on the page itself.
- **StrategySwitch**: the only write (`PUT /scheduler`). Segmented control on a muted ground; active = blue fill, white text; `aria-pressed`, labelled group, error in `role=status`.
- **DivergenceStrip**: SVG `role=img`, fixed 90 s window, newest sample pinned right. Reserved = blue 2.5px line, observed = coral 2.5px line, round joins, dot pen heads. The gap between them is hatched, coral where observed > reserved, slate where reserved > observed. Dashed hairline grid, slate 0% baseline.
- **Finding**: one derived sentence in a rounded panel; observedLight background when `isDiverged`, muted otherwise. Wording and branches unchanged.
- **FleetPlates**: one bordered tile per worker; status chip, 28px coral observed figure, three rounded bars (declared hatched outline, reserved blue fill, observed coral fill; the two measured ones carry `role=meter`).
- **DecisionTrace**: 8 rows with "View all", each expandable (`aria-expanded`) into a candidate table that always shows the observed column, even when null.
- **QueueLedger**: two lists (in the system, friction) with dot markers; nonzero friction rows turn coral.
- **JobsLog / BenchmarkTable**: `.data-table` (13px, semibold headers, hairline rows), status and evidence as chips, sr-only captions, horizontal scroll on narrow screens.
- **Panel states**: loading text "Reading the X…", empty text says what is empty, failures keep stale data and show the "API unreachable · last known" chip.

## Do's and Don'ts

Do:
- Use coral for one meaning: the reading contradicts the register.
- Keep declared, reserved and observed as three separate treatments.
- Derive captions and accent states from the same data the chart draws.
- Give every polled panel loading, empty and failure text; give every SVG or table a label or caption.

Don't:
- Put an em-dash or en-dash in any visible string; empty cells show a hyphen `-`. Use at most one middle dot per line.
- Hand-draw icon paths. Icons come from `@tabler/icons-react` through the wrappers in `Icons.tsx` (stroke 1.75).
- Add decorative dots. A dot must carry state (status pills, chart legend); friction rows signal with coral numbers instead.
- Add the template's other colours, green/red status, or a second accent.
- Add avatars, notifications, search boxes or other template chrome that has no real data behind it.
- Add a chart library or MUI, or any write beyond `PUT /scheduler`.
- Fill declared with a solid colour or blend the three descriptions into one bar.

## Responsive Behavior

- Under `lg` (1024px) the sidebar is an overlay and the content is full width. Tiles: 1 column, 2 at `sm`, 4 at `xl`. Fleet: 1 / 2 at `sm` / 3 at `lg`. Decisions and queue stack under `lg`. Archived-run tables stack under `2xl`.
- The divergence SVG scrolls horizontally below 720px and opens at its live right edge. `body` has `overflow-x: hidden` as a backstop.
- `min-h-[100dvh]`, not `h-screen`. Touch targets: nav items 44px, strategy buttons 36px, ledger rows 48px.

## Iteration Guide

1. Change a colour only in `theme.ts` and `tailwind.config.js` together, then recompute contrast on white (text 4.5:1, graphics and large type 3:1).
2. New section: wrap it in `Card` with an `id`, add the id to `GROUPS` in `Sidebar.tsx`, use `usePoll`, and give it loading/empty/error text.
3. New divergence from expectation: `chip-observed` or `text-observedText`, same size as the happy path.
4. Motion: only data and the one load-in draw. Gate anything new behind `motion-reduce`.
5. Run `cd frontend && npx tsc --noEmit && npm run build`.

## Known Gaps

- Reserved blue #5D87FF is 3.3:1, fine for lines and fills but not for small text; small reserved labels use ink.
- The SVG chart scales with its card, so its axis text gets small on mid-width screens.
- No skeleton loaders; first load is a single "Connecting" line.
- Fonts load from Google Fonts at runtime; fallbacks are system-ui and system mono.
- Verified by typecheck, build, and screenshots at 390px, 936px and 1440px in both themes; no automated visual regression. Headless Chrome with `--force-dark-mode` auto-inverts the page and is not a valid dark-mode check; use the toggle.
