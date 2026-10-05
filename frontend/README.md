# Nimbus dashboard (Phase 6)

Read-only operations view: **live fleet, queue depth, decision trace** — the
Phase 6 exit criterion — plus observed telemetry and the archived benchmark
runs.

React 18 · TypeScript · Vite · Tailwind. Every chart-like element is
hand-rolled SVG — no charting library — so the traces stay in the same
carbon-and-hazard ink as everything else on the page rather than a library's own defaults.

```bash
npm install
npm run dev        # http://localhost:5173, proxies /api -> 127.0.0.1:8000
npm run build      # -> dist/, served by the control plane at /
npm run typecheck
```

Point the dev proxy at another control plane with `NIMBUS_API=http://host:8000 npm run dev`.

Once built, `dist/` is served by the API itself at `/` (`backend/api/app.py`),
so a deployed control plane needs no second process. The API starts fine
without it: the mount is skipped when `dist/` is absent, which is the state
every test in this repo runs in.

## Design

The visual language is deliberate: industrial brutalist, Swiss industrial print
(light). Matte paper, carbon ink, solid black rules, a giant Archivo Black
masthead over IBM Plex Mono data, and one hazard-orange accent
(`frontend/src/lib/theme.ts`). The user replaced the earlier plotter-paper look
with this on 2026-10-05; the ink semantics carried over (graphite became the
declared grey hatch, blue ballpoint became carbon black, red became hazard).
Hazard means the same thing everywhere it appears — a reading that contradicts
the record — whether that record is a CPU reservation, a stale API poll, or a
retried job. There is no green/red status pair and no second accent.

## What the design is doing

**The three state categories never share a colour.** Grey hatch is DECLARED
(operator configuration), solid carbon is RESERVED (Nimbus bookkeeping), hazard is
OBSERVED (psutil). Those are three different claims about a worker and the
entire experiment turns on not confusing them, so a single merged "utilisation"
bar would assert on screen exactly the conflation the project exists to avoid.
The same three-way split extends to job and queue outcomes: things Nimbus
currently holds or is doing normally get carbon ink; anything that
diverged from what was expected — retried, failed, dead-lettered, a tie-break,
a stale poll — gets the same hazard accent, because a divergence from the record is
exactly what that ink already means.

**The header always says whether placement can *read* telemetry.** The
dashboard shows observed CPU whichever strategy is active, because the trace
records it either way. Without that badge, an operator watching a
`static_resource_aware` run would see the scheduler dispatch to a worker sitting
at 100% CPU and reasonably conclude it was broken — rather than seeing the
experimental control working as designed. `adaptive` is the only policy that
receives a view with telemetry fields on it.

**The decision trace shows the observed column even for policies that could not
read it.** That is the point of the trace: a worker at 100% CPU sitting next to
a score computed purely from reservations is the evidence that the boundary
held.

**A stale panel is labelled, never blanked.** On a failed poll the last known
data stays on screen under an "API unreachable" banner. A blank dashboard says
"nothing is happening"; a stale one says "I cannot see" — and only one of those
is usually true.

**Dead letters and truncated runs get the same visual weight as completions.**
Neither raises an error anywhere in the system; both produce a run that looks
perfectly ordinary and reports a slightly wrong number. Three Phase 3 benchmark
runs silently completed 59 of 60 jobs. If those counters are only visible by
scrolling, they may as well not be reported.

## What it deliberately does not do

- **One write, deliberately narrow.** `PUT /scheduler` (via the strategy
  switcher in the header) is the only mutating call this dashboard makes. It's
  safe to expose because it's unauthenticated by design, idempotent, and
  touches nothing but which policy runs next — no job submission, no worker
  controls. Destructive operations still live on the admin router, which is
  not mounted unless `NIMBUS_ENABLE_ADMIN` is set, and Nimbus has no
  authentication before Phase 7 (`docs/limitations.md` §5).
- **No websockets.** Polling on a 2 s interval. The control plane is a single
  scheduler loop that is already a documented throughput bottleneck
  (`docs/limitations.md` §2); a push fan-out would put dashboard traffic on the
  critical path of placement decisions to save three read-only GETs.
- **No CloudWatch.** Everything here is Channel 2 scheduling telemetry read
  through the Nimbus API (`docs/architecture.md` §10). Nothing in the scheduler
  reads CloudWatch, and neither does this.

Design contract: see `/DESIGN.md` at the repo root. Contrast ratios on paper are recorded there (hazard #F54F00 3.07:1 for marks and 24px+ type, #B33A00 5.2:1 for small accent text).
