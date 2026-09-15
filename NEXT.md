# Where things stand

Scratch note for picking the thread back up. Delete it whenever it stops being
useful — the durable record is the README roadmap, `docs/results.md`, and
`docs/aws-batch-baseline.md`.

**State at close:** 498 backend tests pass (untouched this session), frontend
typechecks and builds clean (171kB gzip), `dist/` is current with source,
`docs/results.md` regenerates from 157 archived runs. The dashboard redesign
(see below) is complete and verified end-to-end against a live server.

---

## Dashboard redesign — done

Direction: **plotter paper** — light sage stock, a four-pen ink palette, and
the belief-vs-reality divergence band as the signature. Tokens in
`frontend/src/lib/theme.ts`; rationale in that file's docstring and in
`frontend/README.md`.

Every panel is now in the plotter-paper idiom and wired into `App.tsx`:
`HeaderRail` + `StrategySwitch` (the one write the dashboard makes — `PUT
/scheduler`, unauthenticated, safe), `DivergenceStrip` + `Finding` (the hero),
`FleetPlates`, `DecisionTrace` (rewritten as a ledger — observed column always
rendered, `—` and muted when a policy can't read it, never omitted),
`QueueLedger` (snapshot readout, no chart), `JobsLog`, `BenchmarkTable`
(rewritten — evidence-class badges per profile, reconstructed client-side from
`/benchmarks/runs` + `/benchmarks/profiles` since `/benchmarks/report` only
exposes a two-way primary/descriptive split, not the three-way one
`scripts/generate_report.py` uses). `FleetPanel.tsx` and `TelemetryChart.tsx`
deleted (dead code / superseded). Recharts dependency removed entirely — bundle
is 171kB gzip, down from the pre-redesign 559kB.

New: `scripts/demo_dashboard.py` — a standalone HTTP-driven script (not a
second in-process engine) that applies real CPU pressure to a worker and
streams a `hash_chain`-weighted job mix, for live-seeding the dashboard during
a demo. Never touches `.nimbus/artifacts/benchmarks/` by construction (no
import of the benchmark runner).

**Verified end-to-end** with a live server, the demo script running, and a
mid-run `fifo → adaptive` strategy switch via `PUT /scheduler`: divergence
strip and fleet plates agree on live reserved/observed numbers; the decision
ledger visibly shows the strategy transition (ROUND_ROBIN rows aging out under
fresh ADAPTIVE ones); `Finding` correctly narrates both the "gap" and
"agreement" cases; the benchmark panel's client-side evidence-class
reconstruction exactly matches `docs/results.md`'s published classification
against the real 157-run archive (`homogeneous_saturated`→primary,
`cpu_pressure`/`memory_pressure`→descriptive, `burst`/`heavy_tail`→inadmissible).
The demo script's `--duration` exits cleanly with no orphaned pressure process.

The one thing not directly screenshotted: clicking to expand a decision row in
the ledger (interaction was code-reviewed, not clicked in headless Chrome).

---

## Project work still pending

Ordered by whether it blocks a claim, not by size.

1. **The AWS data plane is a half-bridge.** `worker/api.py` (HTTP worker agent)
   and `backend/api/internal.py` (results intake) exist, but the engine only
   ever constructs `LocalWorkerAgent` and calls `try_execute()` in-process
   (`engine.py:161,327`). There is no scheduler-side HTTP dispatcher, so under
   `NIMBUS_BACKEND=aws` the control plane would still run workers inside its own
   process and never talk to the EC2 fleet. Both modules also have zero test
   coverage. This is a functional hole, not just an untested path.
2. **Phase 5 exit criterion unmet** — `terraform apply` never run, so the AWS
   code path has never executed against the real services.
3. **`worker/batch_entrypoint.py` does not exist**, but `infra/batch.tf:122`
   invokes it. Blocks the Batch baseline.
4. **CloudWatch Channel 1 never built.** `docs/architecture.md` §10 specifies 11
   operational metrics and `infra/iam.tf` grants `PutMetricData` on namespace
   `Nimbus`; nothing writes to it.
5. **`priority` ordering** — unimplemented, deliberately, and documented as such.
6. **Re-run the headline experiment on EC2** — `docs/limitations.md` §3 requires
   it before any result transfers off this machine.
7. **`adaptive-v2`** — the pre-registration itself says the materiality criterion
   should be replaced with a rank-based test plus an effect-size floor.

---

## Two environment gotchas

**The `.venv` is broken and cannot be repaired in place.** This drive is NTFS,
which does not carry POSIX symlinks, so `.venv/bin/python*` are dead files
rather than links. `site-packages` is intact, so everything runs via:

```bash
PYTHONPATH=.venv/lib/python3.14/site-packages:. python3 -m pytest -q
```

Every `.venv/bin/...` invocation in the README will fail. Either keep using the
`PYTHONPATH` form or rebuild the venv somewhere ext4.

**There is no git repository here.** ~18k lines of Python, 1.8k of TypeScript
and 1.5k of Terraform with no version control and no history to fall back on.
Worth `git init` before the next substantial change.
