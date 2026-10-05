# Nimbus — Telemetry-Aware Adaptive Workload Scheduling on AWS

An **experimental** distributed-systems project investigating one question:

> Can an application-level scheduler using real-time worker telemetry, workload
> characteristics, queue state and historical execution data improve scheduling
> efficiency for heterogeneous compute workloads compared with simpler
> scheduling strategies and relevant AWS-native baselines?

**Status: all 9 phases implemented; one exit criterion knowingly unmet.**
Local pipeline, four placement strategies, psutil telemetry, replayable decision
traces, a reliability layer that survives a killed worker, a benchmark API with
derived reports, a read-only operations dashboard, and complete Terraform.
**637 tests pass** (Linux, Python 3.14).

**No AWS resources have ever been provisioned.** `terraform apply` was never
run, so Phase 5's criterion — *the same test suite green against
`NIMBUS_BACKEND=aws`* — is **not met**. The SQS, DynamoDB and S3 adapters are
written and tested against hand-written fakes of those APIs, which pins our
understanding of their semantics and is not the same thing as validating it.
That is also why Phase 8 publishes [no AWS Batch numbers at
all](docs/aws-batch-baseline.md).

Since then the missing pieces have been written, and are equally unrun on AWS:
an HTTP scheduler-to-worker dispatcher (`backend/services/remote.py`), the
Batch entry point (`worker/batch_entrypoint.py`) and CloudWatch operational
metrics (`backend/services/metrics.py`). They are covered by local tests --
HTTP both directions over `httpx.MockTransport` bridges into the real FastAPI
apps -- and `terraform validate` now passes. **"Tested locally" is all that has
been established about any of them.**

**Through Phase 2, telemetry was collected, recorded and exposed — and no
scheduler read it.** That deliberate blindness is what makes Phase 3
measurable. `adaptive-v1` is the first and only policy that consumes telemetry;
the static policies still cannot see it, and `unit/test_views.py` pins that
boundary from both sides so the control cannot rot.

---

## What this is not

AWS already provides production job scheduling. **AWS Batch** has FIFO job
queues, job priority, `dependsOn` dependency DAGs, fair-share scheduling
policies with decay-weighted historical usage, and cost/capacity-aware instance
allocation strategies. **Amazon ECS** has a real placement engine: resource
filtering, binding constraints, and `binpack`/`spread`/`random` strategies.
**SQS** provides lease semantics, retries, DLQs and fair queues. **EC2 Auto
Scaling** provides target-tracking and predictive scaling.

Nimbus is not a replacement for any of them, and the project is forbidden from
claiming otherwise. See [`docs/aws-native-scheduling.md`](docs/aws-native-scheduling.md),
which documents what AWS already solves and then states the narrow thing Nimbus
actually explores.

**The narrow thing:** ECS task placement reasons about *declared* resource
requirements, *reserved* capacity and *static* attributes — its
[cluster query language](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-query-language.html)
has no subject for observed CPU or memory utilization. Batch fair-share reasons
about *consumed queue share per tenant*, not *host pressure*. Nimbus asks
whether adding **observed, high-frequency, per-host telemetry** and
**per-(host, job-type) runtime history** to the per-job placement function
measurably helps — and it treats that as a hypothesis that may be rejected.

---

## The hypothesis

> For heterogeneous workloads running on workers with dynamically changing
> resource utilization, scheduling decisions based on real-time telemetry and
> workload characteristics **may** improve job completion latency, worker
> utilization and load balance compared with simple FIFO or static priority
> scheduling.

The real test is not "does Adaptive beat FIFO" — it should, almost
arithmetically. It is **"does Adaptive beat Static Resource-Aware"**, i.e. does
live telemetry add anything over the reservation accounting ECS already does.
If it does not, that is the finding, and it gets published as such.
[`docs/experiments.md`](docs/experiments.md) pre-registers the sub-hypotheses,
the statistics, and the conditions under which any number may be reported.

---

## Quickstart

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt

# End-to-end demonstration of the Phase 1 pipeline
.venv/bin/python scripts/demo_phase1.py --jobs 24

# Tests
.venv/bin/python -m pytest

# API
.venv/bin/uvicorn backend.api.app:app --reload   # docs at http://127.0.0.1:8000/docs
```

```bash
curl -s -X POST localhost:8000/jobs \
  -H 'content-type: application/json' \
  -d '{"job_type":"prime_count","payload":{"limit":1000000},"priority":2}'

curl -s localhost:8000/workers
curl -s localhost:8000/scheduler
curl -s localhost:8000/scheduler/decisions?limit=1
```

```bash
.venv/bin/python scripts/demo_phase2.py --jobs 40 --trials 3   # Phase 2 demos
.venv/bin/python scripts/run_phase2_baselines.py               # baseline sweep
```

### Benchmarks and the report

```bash
# Queue a sweep. Runs execute one at a time -- two concurrent runs would pin
# two fleets to the same cores and each would measure the other's interference.
curl -s -X POST localhost:8000/benchmarks -H 'content-type: application/json' \
  -d '{"profile":"cpu_pressure","strategies":["fifo","adaptive"],"trials":5}'

curl -s localhost:8000/benchmarks                       # progress
curl -s 'localhost:8000/benchmarks/report?profile=cpu_pressure&fmt=markdown'

# Regenerate every table in docs/results.md from the archive. One command, no
# flags that change the answer -- docs/experiments.md section 8 requires it.
.venv/bin/python scripts/generate_report.py
```

### Dashboard

```bash
cd frontend && npm install && npm run build
.venv/bin/uvicorn backend.api.app:app        # dashboard at http://127.0.0.1:8000/
# or, with hot reload against a running API:
cd frontend && npm run dev                   # http://localhost:5173
```

The dashboard's visual rules (plotter-paper palette, tokens, panel contract) are in [`DESIGN.md`](DESIGN.md).

### Failure simulation

```bash
NIMBUS_ENABLE_ADMIN=1 .venv/bin/uvicorn backend.api.app:app

# Kill a worker for real: the pool is destroyed and its children are killed.
# Nothing tells the control plane; it infers the loss from missing heartbeats
# and re-places the jobs.
curl -s -X POST localhost:8000/admin/workers/worker-1/fail
curl -s localhost:8000/admin/reliability
curl -s -X POST localhost:8000/admin/workers/worker-1/recover
```

### AWS

```bash
pip install -r requirements-aws.txt
export TF_VAR_admin_cidr="$(curl -s https://checkip.amazonaws.com)/32"
scripts/demo/deploy.sh          # ~$0.08/hour
scripts/demo/teardown.sh        # pulls archives down first, then destroys
```

To drive remote workers from a control plane you started yourself, run
`worker/api.py` on each worker host and point the control plane at them. The
venv commands from the quickstart above still apply on each host.

```bash
# on each worker host (identity, slots and where to POST results)
NIMBUS_WORKER_ID=worker-1 NIMBUS_CONTROL_PLANE_URL=http://<control-plane>:8000 \
  uvicorn worker.api:app --host 0.0.0.0 --port 8080

# on the control plane
export NIMBUS_WORKER_TRANSPORT=http
export NIMBUS_WORKER_ENDPOINTS="worker-1=http://10.0.1.11:8080,worker-2=http://10.0.1.12:8080"
uvicorn backend.api.app:app --host 0.0.0.0 --port 8000
```

The endpoint ids become the registered worker ids; `/internal/results` is
mounted whenever the backend is `aws` or the transport is `http`. The worker API
has no authentication beyond the network (security group) in front of it, and
this path has never run on real AWS -- only against local mocks and loopback.
The Terraform in `infra/` wires the same variables for the EC2 fleet.

**Never run against this project's own results.** No AWS resources were ever
provisioned for it (see [`docs/aws-batch-baseline.md`](docs/aws-batch-baseline.md)),
so every number in [`docs/results.md`](docs/results.md) is local.

---

## What Phase 3 found

> **Superseded as the canonical numbers.** The section below is the Phase 3
> write-up as it stood, kept because it is what was claimed at the time.
> [`docs/results.md`](docs/results.md) is now the authority: it is regenerated
> from the archive by a single command, covers all five profiles rather than
> one, and classifies each profile's evidence rather than presenting everything
> as equivalent.
>
> Two things changed under that treatment, and both are worth reading before
> the tables below. The load-regime gate, applied as pre-registered, excludes
> **every** Profile B run — which is the mis-fire the pre-registration itself
> anticipated, so those results are now labelled boundary-condition evidence
> rather than support for H3. And on `homogeneous_saturated`, the only profile
> that qualifies as primary evidence, **no comparison reaches significance** —
> which the pre-registration calls the *good* outcome for that profile, since
> what it tests is whether `adaptive` adds overhead when telemetry carries no
> extra information. It does not.

Profile B: `worker-1` driven to ~100% CPU by processes **outside** Nimbus — no
lease, no slot, no reservation, so Nimbus's declared/reserved model still reports
its full capacity free. Telemetry records the pressure throughout. 34 runs,
N=8–9 per strategy, arms interleaved.

### adaptive-v1 steers away from the pressured worker, consistently

Share of jobs sent to `worker-1` (fair share for 3 workers is 33%):

| Strategy | jobs sent to the saturated worker |
|---|---|
| `static_resource_aware_binpack` | 32.7% ± 5.6% |
| `fifo` | 26.1% ± 3.3% |
| `static_resource_aware` | 18.9% ± **8.9%** |
| **`adaptive`** | **13.1% ± 1.7%** |

The spread matters as much as the mean. Observed CPU is a *stable* signal, so
adaptive holds a steady bias away from the saturated worker. Reservations churn
as jobs start and finish, so `static_resource_aware` oscillates between 12% and
33% across runs.

### That translated into makespan

Makespan under Profile B is bimodal, set almost entirely by a single straggler:
whichever large I/O job lands on the saturated worker runs 10–40× slower there
and dominates the run on its own.

| Strategy | makespan median | per-run makespans (s) | runs with a straggler |
|---|---|---|---|
| **`adaptive`** | **11.6 s** | 11.3 11.4 11.5 11.6 11.7 11.7 12.2 **31.9** | **1 / 8** |
| `static_resource_aware` | 31.0 s | 11.9 12.7 13.3 15.9 31.0 40.4 53.5 53.8 54.4 | 5 / 9 |
| `fifo` | 38.2 s | 11.4 12.4 32.2 36.4 38.2 44.9 45.9 46.1 46.2 | 7 / 9 |
| `static_resource_aware_binpack` | 46.0 s | 11.9 12.4 45.5 45.9 46.0 46.4 46.4 46.7 | 6 / 8 |

Mann-Whitney U, two-sided, Bonferroni-corrected (α = 0.0167 for 3 comparisons):

| Comparison | median delta | p | Cliff's δ |
|---|---|---|---|
| adaptive vs `static_resource_aware` | **−62.5%** | 0.0039 ✓ | −0.83 (large) |
| adaptive vs `fifo` | **−69.5%** | 0.0053 ✓ | −0.81 (large) |
| adaptive vs `binpack` | **−74.7%** | 0.0023 ✓ | −0.91 (large) |

### But the pre-registered test says otherwise, and that has to be reported

The pre-registration required **≥10% median difference AND non-overlapping
per-run ranges**. The median difference is −62.5%; the ranges overlap, because
one adaptive run hit a straggler (31.9 s) and some baseline runs got lucky
(11.4 s). By the letter of the pre-registered criterion this is **"no meaningful
observed difference."**

That criterion was a bad choice, and the flaw is mine: requiring *disjoint
ranges* on a bimodal distribution is nearly unsatisfiable at N≈9 regardless of
effect size. The Mann-Whitney result above follows the project's standing
statistical protocol ([`docs/experiments.md`](docs/experiments.md) §2.3,
written in Phase 1 — non-parametric test, effect size, Bonferroni), so it is not
a criterion invented after seeing the answer. Both verdicts are reported;
neither is dropped.

### One more confound, found by the instrumentation

The static policies decided **35–36% of placements by the `worker_id`
tie-break** rather than by their own scoring — the Phase 2 "R9" degeneracy is
active in the *loaded* regime too, not just at low load as assumed when the load
gate was written. adaptive-v1 ties on **1%** of decisions, because a continuous
observed signal rarely produces exact equality.

So part of adaptive's edge over `static_resource_aware` is "not tying" rather
than telemetry content. `fifo` never ties, which makes **adaptive vs `fifo` the
clean comparison** — and adaptive wins it (−69.5%, p = 0.0053, δ = −0.81).

**Hypothesis: SUPPORTED IN TESTED REGIMES** — under Profile B, with the caveats
above. See [`docs/experiments.md`](docs/experiments.md) §5.7.

### This also corrected a Phase 2 claim

Phase 2 reported "Static Resource-Aware is 73% slower than round-robin" on
Profile B, from **one run per strategy**. Makespan in this profile ranges 11–54 s
for every strategy, so three single draws cannot distinguish schedulers. That
claim was not supported by adequate repetitions and is **withdrawn**.

---

## Documentation

| Document | Contents |
|---|---|
| [`docs/aws-native-scheduling.md`](docs/aws-native-scheduling.md) | What AWS Batch / ECS / SQS / Auto Scaling already provide, with citations; where Nimbus overlaps; where it differs; claims the project is forbidden from making |
| [`docs/architecture.md`](docs/architecture.md) | Control/data plane split, queue and dispatch design, job and worker state machines, DynamoDB access patterns, idempotency, risks, phase plan |
| [`docs/scheduling.md`](docs/scheduling.md) | Scheduler interface, the four strategies, the adaptive scoring function and weight vectors, the decision record |
| [`docs/adaptive-v1-preregistration.md`](docs/adaptive-v1-preregistration.md) | **Phase 3 pre-registration** — adaptive-v1's formula, weights, smoothing window, freshness policy, repetition count and success criteria, fixed before the algorithm was written |
| [`docs/experiments.md`](docs/experiments.md) | Hypotheses, controls, statistics, workload profiles, metric definitions, the AWS Batch fairness conditions |
| [`docs/cost-model.md`](docs/cost-model.md) | Live AWS unit prices, per-job cost derivation, ~$8 project budget, cost controls |
| [`docs/limitations.md`](docs/limitations.md) | Scope, architectural, measurement, comparison and security limitations; open questions |
| [`docs/results.md`](docs/results.md) | **Phase 9 analysis** — every table derived from the archive by `scripts/generate_report.py`, per-profile evidence classes, statistics, cost, conclusions |
| [`docs/aws-batch-baseline.md`](docs/aws-batch-baseline.md) | **Phase 8** — the seven fairness conditions, which are unmet, and the resulting refusal to publish any Batch number |
| [`frontend/README.md`](frontend/README.md) | **Phase 6 dashboard** — what its design is doing, and what it deliberately does not do |
| [`DESIGN.md`](DESIGN.md) | The frontend design contract (plotter-paper palette, tokens, panel rules) the dashboard is built to |

---

## Architecture (as built through Phase 9)

```
   POST /jobs                                     REST API (FastAPI)
        |                                                |
        v                                                v
   validate + classify  ------------------------>  LocalStateStore
        |                                          jobs, decision traces
        v
   LocalJobQueue           <-- SQS semantics: lease, visibility timeout,
   (pending)                   receipt invalidation, redrive to DLQ
        |
        |  the scheduler is the ONLY consumer
        v
   NimbusEngine loop
        |
        +--> OrderingPolicy.select_job()      which job next
        |
        +--> PlacementPolicy.build_views()    <-- INFORMATION BUDGET declared here
        |        WorkerState -> StaticWorkerView    (DECLARED + RESERVED)
        |        WorkerState -> AdaptiveWorkerView  (+ OBSERVED + freshness)
        |
        +--> PlacementPolicy.select_worker()  which worker
        |
        +--> SchedulingStrategy._trace()      <-- telemetry recorded HERE, after
        |                                         the policy has already answered
        v
   push-dispatch
        |
        +------------+------------+
        v            v            v
    worker-1     worker-2     worker-3      LocalWorkerAgent
    2 slots      2 slots      2 slots       ProcessPoolExecutor, pinned cores
        |            |            |
        +------------+------------+
                     |
        TelemetryCollector (psutil, 2 s)   ->  registry.ingest_telemetry()
        HeartbeatEmitter   (5 s)           ->  registry.heartbeat()

   Reaper (2 s)  <-- the failures that produce no event at all
        |
        +--> job past its wall-clock timeout
        +--> job on a worker that stopped heartbeating
        +--> job live in the store with nobody holding its lease
        |
        v
   reclaim  -> release the reservation, abandon the execution,
               then retry with backoff or exhaust to FAILED
```

Every other failure path in Nimbus is *reported* — a workload raises, a worker
rejects a dispatch — and is handled where it lands. The reaper exists for the
ones that produce no event, because nothing else can find them.

**Backends are selected in exactly one place.** `backend/services/factory.py`
is the only module that branches on `NIMBUS_BACKEND`, and
`unit/test_factory.py` enforces that by scanning the source for a second
branch. Swapping `LocalJobQueue` for `SQSJobQueue` changes no line of scheduler
or worker logic — which is what makes "the same tests against a different
substrate" a meaningful claim, on the day someone actually runs them there.

### Three categories of worker state, never conflated

| Category | Type | Source | Readable by placement |
|---|---|---|---|
| **DECLARED** | `DeclaredCapacity` | operator configuration | yes |
| **RESERVED** | `ReservedResources` | Nimbus bookkeeping | yes |
| **OBSERVED** | `WorkerTelemetry` + `ObservedWindow` | psutil | **`adaptive` only** |

### The boundary is structural, not a convention

A placement policy never receives a `WorkerState`. It declares its information
budget through `build_views()`:

```python
@dataclass(frozen=True, slots=True)
class StaticWorkerView:                 # round_robin, static_resource_aware
    worker_id: str;      healthy: bool
    cpu_capacity: int;   memory_capacity: int      # DECLARED
    cpu_reserved: int;   memory_reserved: int      # RESERVED
    slots_total: int;    slots_used: int
    # ...and nothing else. No telemetry field exists here.

@dataclass(frozen=True, slots=True)
class AdaptiveWorkerView:               # adaptive (adaptive-v1)
    # ...the same DECLARED + RESERVED fields, plus:
    cpu_percent: float                  # OBSERVED, smoothed over 3 samples
    memory_percent: float; process_memory_mb: float
    cpu_trend_pct_per_s: float; load_average: float | None
    telemetry_age_s: float | None; telemetry_samples: int
    telemetry_fresh: bool               # never silently trusted
```

For a static policy there is no telemetry field, so reading one is an
`AttributeError`, not a judgement call. `slots=True` removes `__dict__`, so
telemetry cannot be attached at runtime either. `tests/unit/test_views.py` pins
both field sets and asserts the static one contains none of the observed ones —
Phase 3 added a telemetry-consuming policy **without** weakening that guarantee.

Both views share the same DECLARED/RESERVED field names, so a single feasibility
filter serves both: telemetry ranks candidates, it never changes eligibility.

This replaces the Phase 1 arrangement, where `place()` received a view carrying
`cpu_usage` and a promise not to read it. **A promise is not a control** — one
stray `worker.observed.cpu_percent` and the baseline silently becomes
telemetry-aware, the experiment loses its control, and nothing fails.

### Other design points

**The scheduler is the sole queue consumer, and dispatch is a push.** The
obvious SQS pattern — N workers long-polling one queue — is *competing
consumers*, where placement is decided by whoever polls first. That cannot
implement telemetry-aware placement and would make every strategy comparison
meaningless.

**Workloads run in processes, not threads,** with BLAS pinned to one thread per
job. Under the GIL, three "workers" in one interpreter cannot contend for CPU
the way three EC2 instances do.

**Heartbeats are independent of telemetry.** Liveness must work with telemetry
disabled, or the ON/OFF control arm would have no healthy fleet.

## Scheduling strategies

Ordering and placement are **separate objects**, composed by a
`SchedulingStrategy`. Every implemented strategy uses FIFO ordering, so any
measured difference between them is attributable to placement alone.

| Strategy | Ordering | Placement | Sees | Status |
|---|---|---|---|---|
| `fifo` | `fifo` | `round_robin` | DECLARED + RESERVED | **implemented** |
| `static_resource_aware` | `fifo` | balance on projected utilisation | DECLARED + RESERVED | **implemented** |
| `static_resource_aware_binpack` | `fifo` | ECS-style binpack | DECLARED + RESERVED | **implemented** |
| `adaptive` | `fifo` | adaptive-v1 weighted score | **+ OBSERVED** | **implemented** |
| *priority* | *priority + aging* | *round_robin* | — | not implemented |

Every strategy uses **FIFO ordering**, so a comparison isolates placement. The
Phase 1 design sketched adaptive with priority ordering; that was dropped so
Static-vs-Adaptive varies exactly one thing — the information available to
placement. `build_scheduler("priority")` still raises; the API returns 422
listing what actually exists.

**adaptive-v1** adds observed host state to that score, with weights frozen in
`docs/adaptive-v1-preregistration.md` before the code was written:

```
Score(w,j) = α · S_cpu          (observed, smoothed over 3 samples)
           + β · S_memory       (observed)
           + γ · S_reservation  (declared + reserved — the static model, kept)
           + δ · S_queue        (declared + reserved)
           + ε · S_trend        (observed, least-squares slope)
```

| Class | α CPU | β Mem | γ Res | δ Queue | ε Trend |
|---|---|---|---|---|---|
| `CPU_BOUND` | **0.50** | 0.10 | 0.20 | 0.15 | 0.05 |
| `MEMORY_BOUND` | 0.10 | **0.50** | 0.20 | 0.15 | 0.05 |
| `IO_BOUND` | 0.15 | 0.15 | 0.25 | **0.40** | 0.05 |
| `LIGHTWEIGHT` | 0.10 | 0.05 | 0.25 | **0.55** | 0.05 |

It is Static **plus** observation, never observation alone — Nimbus knows about
work it has dispatched that telemetry has not caught up with yet. Stale or
missing telemetry falls back to reservation-derived estimates, never to "0% CPU"
(which would make the least-known worker the most attractive one). Feasibility
is still evaluated on static fields only: telemetry ranks, it never changes
eligibility, and there is no `if cpu > 90` cliff anywhere.

**Static Resource-Aware** scores each feasible worker on projected utilisation
after placement, weights frozen in source before any experiment ran:

```
StaticScore(w,j) = 0.40 * (cpu_reserved    + cpu_units) / cpu_capacity
                 + 0.30 * (memory_reserved + memory_mb) / memory_capacity
                 + 0.30 * (slots_used      + 1)         / slots_total
```

Lowest wins; ties break on `worker_id`. The objective is **balance, not
binpack** — deliberately, so that the Phase 3 comparison varies only the
*information source* (declared vs observed) and not the *objective* (pack vs
spread). Binpack ships as a separate labelled configuration because it is the
faithful ECS analogue.

The adaptive score, weight vectors and the anti-herding correction are fully
specified in [`docs/scheduling.md`](docs/scheduling.md) §5 — designed before
implementation, and committed before any experiment runs, so the weights cannot
be quietly tuned to the benchmark.

`select_next()` (ordering) and `place()` (placement) are deliberately orthogonal
so the benchmark can isolate the contribution of telemetry from the contribution
of priority ordering. `place()` is a pure function of an immutable fleet
snapshot, which is what makes strategies unit-testable and decision traces
replayable.

## Explainable decisions

Every placement — including ones that placed nothing — persists the candidate
set, the scores, the exclusion reasons, the runner-up and the margin:

```json
{
  "job_id": "job-e45f9cc8bfda",
  "ordering_policy": "fifo", "placement_policy": "static_resource_aware",
  "telemetry_enabled": true,
  "weights": { "cpu": 0.40, "memory": 0.30, "slots": 0.30 },
  "candidates": {
    "worker-1": {
      "eligible": false, "ineligible_reason": "no free slot: 2/2", "healthy": true,
      "declared_cpu_units": 2048, "declared_memory_mb": 4096, "declared_slots": 2,
      "reserved_cpu_units": 2048, "reserved_memory_mb": 128, "active_jobs": 2,
      "observed": { "cpu_percent": 100.0, "memory_percent": 77.5,
                    "load_average_1m": 5.28, "process_memory_mb": 80.49 },
      "score": null, "terms": {}
    },
    "worker-3": {
      "eligible": true, "healthy": true,
      "reserved_cpu_units": 1024, "active_jobs": 1,
      "observed": { "cpu_percent": 29.25, "...": "..." },
      "score": 0.7093750000000001,
      "terms": { "projected_cpu": 1.0, "projected_memory": 0.03125,
                 "projected_slots": 1.0, "contrib_cpu": 0.4,
                 "contrib_memory": 0.009375, "contrib_slots": 0.3 }
    }
  },
  "selected_worker": "worker-3", "decision_latency_us": 20.265,
  "reason": "lowest static resource score (balance): worker-3=0.7094 [...]"
}
```

Note `worker-1` at **100% observed CPU** sitting in the trace next to a `terms`
block containing only `projected_*` values. The telemetry was recorded; it was
not an input.

**Traces replay.** `scheduler/replay.py` rebuilds a decision's inputs from its
recorded DECLARED and RESERVED fields and re-executes the policy, which must
reproduce the recorded worker and every score. Across the telemetry ON/OFF
control: **240/240 decisions replayed exactly**. If placement had consumed
observed state, telemetry-on traces would fail to replay from static inputs
alone.

---

## Workloads

Nine parameterised, deterministic, seeded workloads across all four classes.
Job types are a **closed registry** — a payload chooses parameters, never code —
and every parameter is bounded, because a payload decides how much CPU and
memory a worker allocates.

| Class | Job types |
|---|---|
| `CPU_BOUND` | `matrix_multiply`, `prime_count`, `hash_chain` |
| `MEMORY_BOUND` | `sort_large`, `array_transform` |
| `IO_BOUND` | `compress_file`, `file_scan` |
| `LIGHTWEIGHT` | `json_transform`, `metadata_extract` |

`GET /workloads` returns the catalogue with each type's parameter schema.

---

## Testing

```
637 passed
```

| Suite | Tests | Covers |
|---|---|---|
| `unit/test_job_state_machine.py` | 9 | Every `(status, event)` pair; terminal states have no exits |
| `unit/test_local_queue.py` | 16 | Visibility timeout, receipt invalidation, ack/nack/extend, DLQ redrive, atomic deferral and its separate retry budget |
| `unit/test_workloads.py` | 35 | Determinism, payload bounds, arithmetic correctness |
| `unit/test_executor.py` | 5 | BLAS thread restriction, failure captured as data |
| `unit/test_fifo_scheduler.py` | 16 | FIFO ordering, round-robin rotation, feasibility filter, aging |
| **`unit/test_views.py`** | 17 | **Telemetry boundary: both view field sets pinned, adaptive view a strict superset, static view still telemetry-free after Phase 3** |
| **`unit/test_static_placement.py`** | 23 | **Feasibility, frozen formula, determinism, reservation feedback, telemetry isolation** |
| **`unit/test_telemetry.py`** | 18 | **Sampling, core scoping, overhead, heartbeat independence, staleness → UNHEALTHY** |
| **`unit/test_replay.py`** | 19 | **Trace replay, tamper detection, and counterfactuals in both directions: static traces re-scored under adaptive and vice versa** |
| **`unit/test_adaptive_placement.py`** | 33 | **adaptive-v1: pinned version and weights, bounded score, smoothing and trend, stale/missing telemetry fallback, telemetry cannot override feasibility or health, deterministic tie-break, full component trace** |
| **`unit/test_generator.py`** | 27 | **Profile determinism, payload validity, burst/pareto shapes** |
| **`unit/test_metrics.py`** | 14 | **Utilisation CV vs job-count CV, Jain fairness, latency decomposition, switch rate, herding concentration, pre-registered load-regime gate** |
| `integration/test_pipeline.py` | 18 | Real engine, real processes: completion, slot limits, DLQ regression, `wait_for_idle` respects in-flight job state |
| `integration/test_api.py` | 13 | Full HTTP surface through the real lifespan, including adaptive selection and placement |
| **`integration/test_telemetry_isolation.py`** | 11 | **ON/OFF control, replay determinism, external CPU pressure observed but not acted on by static policies** |
| **`unit/test_reaper.py`** | 20 | **Timeout vs worker-loss vs orphan classification, the grace period that stops it racing dispatch, draining excluded, DLQ reconciliation** |
| **`unit/test_retry.py`** | 12 | **Exponential backoff, the cap as a genuine bound, jitter deterministic in `(job_id, attempt)`** |
| **`unit/test_aws_adapters.py`** | 39 | **SQS/DynamoDB/S3 against hand-written fakes: receipt expiry → `LeaseExpired`, `release` keeps the delivery count and `defer` resets it, conditional writes → `ConditionFailed`, no-scan listing** |
| **`unit/test_factory.py`** | 13 | **Exactly one place branches on the backend — asserted by scanning the source; boto3 never imported at module scope; no credential field exists in the config** |
| **`unit/test_analysis.py`** | 33 | **Exact Mann-Whitney null distribution verified against brute-force enumeration to 6×6, Cliff's δ, seeded bootstrap, the published Phase 3 effect sizes pinned** |
| **`unit/test_report.py`** | 23 | **Derivation from the archive: truncated and low-load exclusion, the load gate's mis-fire on pressure profiles, tie-break rate from decision traces** |
| **`unit/test_cost.py`** | 14 | **Projection labelled as one, and the check that a cost table ranking strategies like the makespan table is reported as a restatement, not corroboration** |
| **`integration/test_admin_api.py`** | 14 | **The admin gate: no admin handler runs when the router is unmounted, and the fleet is provably untouched** |
| **`integration/test_benchmark_api.py`** | 15 | **`POST /benchmarks` → archived `raw.jsonl` + derived report; serialised execution; cancellation at cell boundaries** |
| **`failure/test_worker_failure.py`** | 7 | **Kill a worker for real — pool destroyed, children killed, heartbeat stopped — and the job completes elsewhere. The loss is *detected*, never announced** |
| **`failure/test_retry_and_timeout.py`** | 17 | **Timeout → reclaim → retry → exhaust; a rejected dispatch never costs a retry; a superseded attempt's result is discarded; reservations release exactly once** |
| **`unit/test_remote_dispatch.py`** | 46 | **HTTP dispatcher over `MockTransport`: endpoint parsing, 202/409/5xx/timeout/identity-mismatch handling, `/health` liveness, remote telemetry collector, factory transport selection, engine on `worker_transport=http`** |
| **`unit/test_worker_api.py`** | 17 | **Worker HTTP API against a fake agent: `/execute` 202/409/422, `/abandon`, `/health`, `/telemetry`, `/stats`; `ResultReporter` retry and drain; Linux-only real-agent run** |
| **`unit/test_internal_api.py`** | 15 | **`/internal/results` intake: validation, defaults, real engine completes once, duplicate and superseded results discarded, mounted only for `aws` or `http`** |
| **`unit/test_batch_entrypoint.py`** | 19 | **Batch entry point: exit-code contract, argument parsing, BLAS pinned before numpy, shared `worker.executor.execute`, scratch cleanup, subprocess smoke, `batch.tf` command contract** |
| **`unit/test_cloudwatch_metrics.py`** | 20 | **Metric names, namespace pinned to the IAM condition, per-series aggregation into StatisticSets, 20-datum batching, throttling dropped without raising, no module-scope boto3** |
| **`unit/test_engine_metrics.py`** | 14 | **Engine emission points, replay counted as a duplicate not a completion, gauge sampling, `metrics_enabled=False`, flush on stop** |
| **`integration/test_http_dataplane.py`** | 8 | **HTTP both directions through bridges into the real FastAPI apps: completion, 409 costs no attempt, duplicate result, timeout triggers `/abandon`, unhealthy `/health`; one Linux-only variant with real `LocalWorkerAgent`s** |

**Phase 1: 104. Phase 2 added 110. Phase 3 added 60. Phases 4–9 added 224.
Total 498** — 401 unit, 73 integration, 24 failure. **The HTTP data plane, batch
entry point and metrics work added 139 more: 637 total** — 532 unit, 81
integration, 24 failure (collected count; all pass on Linux). No test has ever been
deleted; the 16 Phase 1 scheduler tests were ported to the ordering/placement
split with every assertion preserved. The Phase 3 additions
are concentrated in `test_adaptive_placement.py` (33), with the rest covering
the five bugs listed below.

### The critical regression test

```python
def test_static_placement_ignores_observed_cpu_pressure():
    # worker-1 wins on static score but is observed at 99% CPU.
    # worker-2 loses on static score but is observed nearly idle.
    assert decision.selected_worker == "worker-1"        # still picks worker-1
    assert decision.observed_cpu("worker-1") == 99.0     # and the trace knows
```

That "weakness" is the control. Without it, Phase 3 cannot measure whether
telemetry adds information.

### Bugs found by running the code

Phases 4–9 (six):

- **A dispatch rejection consumed the retry budget.** `attempts` was incremented
  at SCHEDULE time and never rolled back when the worker answered "at capacity",
  so a burst that momentarily exceeded fleet capacity could walk a perfectly
  healthy job to `FAILED`. The same mistake as the Phase 1 deferral bug,
  arriving through `Job.attempts` instead of through the queue's delivery count.
- **Redrive on the receive path told nobody.** `receive()` moves an over-limit
  message to the DLQ on its way past, but only `sweep_expired()` reported
  anything — so a job dead-lettered during a receive kept a live job record with
  no message left to deliver it. A permanent, silent stall. The queue now tracks
  redrives from both paths and the reaper reconciles them.
- **`crash()` politely waited for its victim.** `ProcessPoolExecutor.shutdown`
  clears `_processes` and terminates cooperatively, so the "kill a worker" test
  was killing nothing until the job finished on its own. Child handles are now
  captured before shutdown and killed.
- **The load-regime gate mis-fired on its own headline experiment.** Applied as
  written, it excludes **every** Phase 3 pressure run: a scheduler that
  successfully steers work away from a saturated worker leaves it idle from
  Nimbus's point of view, so measured fleet utilisation *falls as a consequence
  of the effect being studied*. The pre-registration had already recorded this;
  the report now detects it, and only relaxes the gate on profiles that actually
  apply pressure.
- **The cost "restatement" check tested the wrong quantity.** It keyed off
  compute's *share of the total*, which at ten-second benchmark scale is a
  minority — so it reported "not a restatement" while the cost table went on
  ranking strategies in exactly makespan order. It now compares the two
  orderings directly.
- **An exclusion message that contradicted itself.** `mean fleet utilisation
  0.50 < 0.50`, because the message rounded to two decimals the value it was
  justifying. Cosmetic, and it made a working gate look broken.

Phase 3 (five):

- **Deferral race.** Deferral was ack-then-enqueue, leaving a window where a job
  existed in neither the queue nor a worker. A drain check landing in that gap
  declared a running benchmark finished — **three 60-job runs silently completed
  59**. Deferral is now a single atomic queue operation.
- **`wait_for_idle` checked queue depth and worker busyness but not job state**,
  so a job in transit between control-plane steps counted as finished.
- **Traces recorded telemetry but no freshness verdict for static policies**, so
  an archived static trace could not be re-scored counterfactually under
  adaptive — every worker looked stale. Freshness is now recorded for every
  policy.
- **The strategy registry hardcoded adaptive's freshness threshold** and
  special-cased it by name, so a configured value was silently ignored.
- **Negative margins for binpack.** Runner-up was computed assuming a minimising
  objective, so a maximising policy reported the margin against the candidate
  *furthest* from its own choice.

Phase 2 (three):

- **The config alias silently dropped values.** Adding `validation_alias` to
  `telemetry_interval_s` disabled population by field name, so
  `NimbusConfig(telemetry_interval_s=0.5)` silently kept the 2.0 default — which
  would have quietly invalidated any experiment that varied the sampling
  interval. Fixed with `populate_by_name=True`.
- **The trace didn't record the placement *mode*.** Both static modes shared the
  class name `static_resource_aware`, so a binpack decision replayed as a balance
  decision and disagreed with itself. The policy name now encodes the mode.
- **The first ON/OFF control test asserted the wrong invariant.** It required
  bit-identical placement sequences. Measurement showed two telemetry-**OFF**
  runs also differ (13 of 24 placements), because reservation state at decision
  time depends on real completion timing. The right claim — each decision is a
  deterministic function of its recorded inputs — is now tested by replay.

Phase 1 (three): DLQ starvation from deferral counting as delivery; unreachable
slots from declared CPU capacity below `slots x max job cpu_units`; multi-threaded
BLAS letting one job consume six cores.

## Roadmap

| Phase | Deliverable | Status |
|---|---|---|
| 1 | Local API, queue, FIFO scheduler, 3 workers, real workloads | **done** |
| 2 | Ordering/placement split, Static Resource-Aware, telemetry, decision traces + replay, benchmark harness | **done** |
| 3 | adaptive-v1: telemetry-weighted placement, freshness handling, adaptive replay, algorithm versioning | **done** |
| 4 | Reaper, timeouts, retries, idempotency, DLQ handling | **done** |
| 5 | Terraform: EC2, SQS, DynamoDB, S3, CloudWatch, IAM | **code complete, never applied** |
| 6 | Operations dashboard (React + TS + Vite + Tailwind + Recharts) | **done** |
| 7 | Benchmark framework | **done** |
| 8 | AWS Batch baseline, or a documented refusal to publish an unfair one | **done — a refusal** |
| 9 | Analysis: tables, figures, statistics, cost, limitations, conclusions | **done** |

**Phase 5 is the one with an unmet exit criterion**, and it is stated rather than
rounded up. The criterion was *the same test suite green against
`NIMBUS_BACKEND=aws`*; `terraform apply` was never run, so the AWS code path has
never executed against the real services. `infra/` is complete and the adapters
are covered by unit tests against hand-written fakes of the SQS, DynamoDB and S3
surfaces — which pins what we believe those semantics are, and is not the same
as confirming it.

Since that was first written, the three things Phase 5 was missing in code have
been added: the scheduler-side HTTP dispatcher and remote telemetry/liveness
(`backend/services/remote.py`, selected by `NIMBUS_WORKER_TRANSPORT=http`),
`worker/batch_entrypoint.py`, and CloudWatch Channel 1
(`backend/services/metrics.py`); `infra/` was wired for the HTTP path and now
passes `terraform fmt -check` and `terraform validate` (validate caught a real
bug: `aws_batch_compute_environment` used `name`, which provider 5.x rejects).
All of it is code-complete and tested locally, **none of it has run on AWS**, and
the exit criterion above is exactly as unmet as before.

Still not built, and still deliberately: `docker-compose.yml`. The whole system
is one process locally, and composing it would be theatre.

---

## Security posture

Single-tenant and unauthenticated by design in Phases 1–7; bound to localhost
locally and to a private security group on AWS. Not a production posture — see
[`docs/limitations.md`](docs/limitations.md) §5. Payloads are bounded and
validated; job types are a closed enum; there is no `eval` and no dynamic import
from a payload. Administrative and simulation endpoints live on a separate
router gated by `NIMBUS_ENABLE_ADMIN`, and are **not registered at all** when it
is unset — with no authentication anywhere, "the route does not exist" is the
entire access-control story for endpoints that can crash a worker. The Phase 6
dashboard is read-only and can reach none of them.

`.env` is git-ignored, and no credential is readable from configuration at all:
`NimbusConfig` holds resource *addresses* only, so there is no path by which a
key could reach a config file, a log line or a benchmark manifest. On AWS,
credentials come from instance profiles, and the worker IAM role has no SQS and
no DynamoDB access — so the control/data-plane split is enforced by IAM rather
than by convention.
