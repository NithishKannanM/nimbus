# Where things stand

Scratch note for picking the thread back up. Delete it whenever it stops being
useful — the durable record is the README roadmap, `docs/results.md`, and
`docs/aws-batch-baseline.md`.

**State at close:** 637 backend tests pass on Linux, Python 3.14 (the HTTP data plane, batch entry point and metrics work added 139; see items 1/3/4), frontend
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

1. **HTTP data plane -- done, never run on AWS.** `backend/services/remote.py`
   (`RemoteWorkerClient`, `RemoteTelemetryCollector`) is selected by
   `NIMBUS_WORKER_TRANSPORT=http` + `NIMBUS_WORKER_ENDPOINTS`; `/internal/results`
   is mounted for `aws` or `http`; `infra/ec2.tf` gives the control plane a fixed
   private IP and the workers their id and control-plane URL. Covered by
   `test_remote_dispatch.py`, `test_worker_api.py`, `test_internal_api.py` and
   `integration/test_http_dataplane.py` (HTTP both ways over `MockTransport`
   bridges into the real FastAPI apps). **Remains:** an AWS run -- a
   control plane and workers on separate hosts, with real latency, security
   groups and partial failure. Remote `fail_worker(hard=True)` is deliberately
   `NotImplementedError`.
2. **Phase 5 exit criterion unmet** — `terraform apply` never run, so the AWS
   code path has never executed against the real services.
3. **`worker/batch_entrypoint.py` -- done, never run on AWS.** It calls the same
   `worker.executor.execute` the pool child runs (exit 0 ok / 1 workload failed /
   2 usage; `batch.tf` does not retry on 2). `test_batch_entrypoint.py` includes
   a subprocess smoke run. **Remains:** build and push an image, apply with
   `enable_batch_baseline = true`, and run it under real Batch. The Batch
   baseline is still unpublished: all the other conditions in
   `docs/aws-batch-baseline.md` stay unmet.
4. **CloudWatch Channel 1 -- done, never run on AWS.** `backend/services/metrics.py`
   (`CloudWatchMetricsSink`) and the engine's emission points write the 11
   metrics of `docs/architecture.md` §10 to namespace `Nimbus`, aggregated per
   series into StatisticSets. Tested against a hand-written `FakeCloudWatch`,
   which pins our reading of `PutMetricData`, not CloudWatch's behaviour.
   **Remains:** an AWS run to see the series actually appear and the IAM
   namespace condition accept them.
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

**Git.** A repository exists (one initial commit); the HTTP data plane, batch
entry point, metrics and Terraform changes above are uncommitted working-tree
edits. An earlier version of this note said there was no repository -- that was
wrong.
