# Nimbus Scheduling Design

Companion to [`architecture.md`](./architecture.md). Defines the scheduler
interface, the four strategies, the adaptive scoring function, and the decision
record.

---

## 1. Two decisions, not one

**Status: implemented (Phase 2).**

Every scheduler makes **two** independent choices, and conflating them is the
easiest way to produce an uninterpretable benchmark:

| Decision | Question | Interface |
|---|---|---|
| **Ordering** | Which queued job is considered next? | `OrderingPolicy.select_job()` |
| **Placement** | Which worker runs it? | `PlacementPolicy.select_worker()` |

These are **separate objects**, not two methods on one class, composed by a
`SchedulingStrategy`:

```
Queue ──► OrderingPolicy.select_job() ──► job ──► PlacementPolicy.select_worker() ──► worker
```

Every strategy implemented so far uses `FIFOOrdering`, so any measured
difference between them is attributable to placement alone. That is the point:
`static_resource_aware` differs from `fifo` in exactly one axis.

| Strategy name | Ordering | Placement | Status |
|---|---|---|---|
| `fifo` | `fifo` | `round_robin` | implemented (Phase 1) |
| `static_resource_aware` | `fifo` | `static_resource_aware` (balance) | **implemented (Phase 2)** |
| `static_resource_aware_binpack` | `fifo` | `static_resource_aware` (binpack) | **implemented (Phase 2)** |
| `adaptive` | `fifo` | `adaptive` (adaptive-v1) | **implemented (Phase 3)** |
| *priority* | *priority + aging* | *round_robin* | not implemented |

`adaptive` uses **FIFO ordering like every other strategy**. The Phase 1 design
sketched it with priority ordering; that was dropped so the Static-vs-Adaptive
comparison varies exactly one thing — the information available to placement.
Priority ordering remains unimplemented and `build_scheduler("priority")` still
raises; the API returns 422 listing what actually exists.

---

## 2. Three categories of worker state

The whole experiment turns on keeping these apart, so they are separate types,
not separate fields on one object:

| Category | Type | Source | Who may read it |
|---|---|---|---|
| **DECLARED** | `DeclaredCapacity` | operator configuration | any policy |
| **RESERVED** | `ReservedResources` | Nimbus's own bookkeeping | any policy |
| **OBSERVED** | `WorkerTelemetry`, `ObservedWindow` | psutil | **`adaptive` only** |

`WorkerState` holds all three. A placement policy never receives a
`WorkerState` — it receives a projection it declares via `build_views()`:

| Policy | View | Sees |
|---|---|---|
| `round_robin`, `static_resource_aware` | `StaticWorkerView` | DECLARED + RESERVED |
| `adaptive` | `AdaptiveWorkerView` | DECLARED + RESERVED + OBSERVED + telemetry quality |

Phase 3 added the second row **without weakening the first**: the static view
still has no telemetry fields, and `tests/unit/test_views.py` pins both field
sets and asserts the static one contains none of the observed ones.

---

## 3. The policy interfaces, and the telemetry boundary

```python
class OrderingPolicy(ABC):
    def select_job(self, jobs: Sequence[Job], now: datetime) -> Job | None: ...

class PlacementPolicy(ABC, Generic[V]):
    def build_views(self, workers: Sequence[WorkerState]) -> tuple[V, ...]: ...
    def select_worker(self, job: JobRequirements,
                      workers: Sequence[V], now: datetime) -> PlacementOutcome: ...
```

`build_views` is how a policy **declares its information budget**. The engine
hands over the full fleet snapshot exactly once, there; `select_worker` only
ever sees the projection.

```python
class StaticPlacementPolicy(PlacementPolicy[StaticWorkerView]):
    def build_views(self, workers):
        return static_fleet_view(tuple(workers))   # DECLARED + RESERVED only
```

```python
@dataclass(frozen=True, slots=True)
class StaticWorkerView:
    worker_id: str
    healthy: bool
    cpu_capacity: int      # DECLARED
    memory_capacity: int   # DECLARED
    cpu_reserved: int      # RESERVED
    memory_reserved: int   # RESERVED
    slots_total: int       # DECLARED
    slots_used: int        # RESERVED
```

**There is no telemetry field, so reading one is an `AttributeError`, not a
judgement call.** `slots=True` removes `__dict__`, so telemetry cannot be
attached to an instance at runtime either. `tests/unit/test_views.py` pins the
exact field set, so adding an observed field fails the suite.

This replaces the Phase 1 arrangement, where `place()` received a `WorkerView`
carrying `cpu_usage` and a promise not to read it. A promise is not a control.

### 3.1 Why the static baseline must stay blind

The Phase 3 comparison is **Static Resource-Aware vs Telemetry-Aware Adaptive**.
It only measures anything if the static arm genuinely cannot see observed state.
One stray `worker.observed.cpu_percent` in a static policy and the baseline
quietly becomes telemetry-aware, the control is lost, and *nothing fails* — the
numbers would simply mean something other than what the report claims. That
failure is silent, which is why the boundary is structural rather than a
convention.

Phase 3 adding a telemetry-consuming policy did not relax this. It added a
*second* view type; it did not add fields to the first. The information budget
of every policy is still declared by the single `build_views()` call, and
`tests/unit/test_views.py::test_static_view_still_has_no_telemetry_after_phase_3`
exists specifically to catch erosion.

### 3.2 Recording is not consuming

Every decision trace records observed telemetry for every worker. It is written
by `SchedulingStrategy._trace`, from the full snapshot, **after** the policy has
already returned its answer. Recording and consuming are different code paths,
not different intentions.

`scheduler/replay.py` verifies this end to end. A *static* decision is
re-executed from its recorded DECLARED and RESERVED fields alone and must
reproduce the recorded worker and every score; if a static policy had consumed
observed state, a telemetry-on trace would fail to replay from static inputs.

From Phase 3, traces also record `telemetry_fresh` and the smoothed
`observed_window` for **every** policy, not just adaptive ones — so an archived
static trace can be re-scored counterfactually under adaptive-v1 and vice versa,
without re-running the workload. (Recording freshness only for adaptive traces
was a real gap, found by a counterfactual replay test that could not work.)

---

## 4. Feasibility filter (shared by all policies)

Applied before any ranking, in one place:

1. `healthy` — `UNHEALTHY`, `DRAINING`, `DEAD` are never candidates.
   Health comes from heartbeat liveness, not telemetry: excluding a dead worker
   is a correctness requirement for *every* strategy, not an experimental
   variable.
2. `slots_free > 0`.
3. `cpu_available >= job.cpu_units`.
4. `memory_available >= job.memory_mb`.

Steps 2–4 are the same declared-requirement fit test ECS performs in its step 1
(`aws-native-scheduling.md` §2). If the filter empties the candidate set the
policy returns `selected_worker=None` and the engine defers the job.

---

## 5. Implemented placement policies

### 5.1 `round_robin` — the Phase 1 baseline

Rotates over eligible workers in `worker_id` order. Round-robin rather than
"first free": the latter degenerates to always-worker-1 under light load, which
would make the baseline a straw man.

It is **stateful** (a rotation cursor), so its decisions are not replayable from
a trace. `scheduler/replay.py` raises `NotReplayable` rather than pretending.

### 5.2 `static_resource_aware` — the Phase 2 baseline

Reasons about DECLARED capacity and Nimbus RESERVATIONS. Structurally incapable
of reading telemetry.

**Formula, frozen in source before any experiment ran:**

```
projected_cpu(w, j)   = (cpu_reserved(w)    + cpu_units(j))  / cpu_capacity(w)
projected_mem(w, j)   = (memory_reserved(w) + memory_mb(j))  / memory_capacity(w)
projected_slots(w)    = (slots_used(w)      + 1)             / slots_total(w)

StaticScore(w, j) = 0.40 * projected_cpu
                  + 0.30 * projected_mem
                  + 0.30 * projected_slots
```

Weights sum to 1 and every term is in [0, 1], so `StaticScore` ∈ [0, 1] and is
comparable across jobs, workers and runs.

| Weight | Value | Rationale |
|---|---|---|
| CPU | **0.40** | Most contended declared resource, and the finest-grained: job `cpu_units` range 128–1024 across the workload registry. |
| Memory | **0.30** | Coarser, but it is the hard constraint that actually fails. |
| Slots | **0.30** | The only term identical across job types, which keeps the score meaningful when declared requirements are uniform. |

Zero capacity projects as `1.0`, so a misconfigured worker cannot become the
most attractive candidate by dividing by zero.

**Tie-break:** lowest score, then lexicographically smallest `worker_id`.
Deterministic and independent of input order. See §5.4 for the measured
consequence of that choice.

### 5.3 Why the objective is *balance*, not *binpack*

This is a deliberate change from the Phase 1 design document, which specified
Scheduler C as ECS-style `binpack` ("minimise post-placement waste"). Those are
**opposite** rules: minimising projected utilisation picks the *least* loaded
feasible worker (spread); minimising leftover capacity picks the *most* loaded
one that still fits (pack).

Balance is the default because the Phase 3 comparison is
Static Resource-Aware vs Adaptive, and Adaptive's whole thesis is "steer away
from loaded workers". If the static baseline packed while Adaptive spread, the
experiment would vary two things at once — the **objective** (pack vs spread)
and the **information source** (declared vs observed) — and any difference would
be uninterpretable. Holding the objective constant makes the information source
the only variable, which is the entire point.

`binpack` remains available as `static_resource_aware_binpack`, a separate
**labelled configuration**, because it is the faithful ECS analogue that
`aws-native-scheduling.md` compares against. It is never a silent default swap.

The policy's `name` encodes its mode (`static_resource_aware` vs
`static_resource_aware_binpack`). This is load-bearing: both modes share an
implementation but make opposite choices, and a trace recording only the class
name replayed a binpack decision as a balance decision — a real bug, caught by
`test_binpack_traces_replay_under_binpack`.

### 5.4 Measured behaviour: it depends entirely on the load regime

Two Phase 2 sweeps, same code, different offered load. Both are single-trial and
therefore descriptive, not inferential.

**Loaded (Profile A, 60 jobs, per-worker utilisation 0.71–0.82):** the policy
works as designed and balances *better* than round-robin.

| Strategy | makespan | thr/s | util CV | job-count CV |
|---|---|---|---|---|
| `fifo` (round-robin) | 8.71 s | 6.89 | 0.0697 | 0.4924 |
| `static_resource_aware` | 9.30 s | 6.45 | **0.0308** | 0.2179 |
| `static_resource_aware_binpack` | 12.11 s | 4.96 | 0.0589 | 0.2784 |

Note binpack's 39% worse makespan: packing onto fewer workers costs throughput
when there is enough work to fill the fleet.

**Lightly loaded (same profile, 40 jobs at 0.4 size scale, fleet utilisation
≈0.2):** the policy degenerates. When the fleet is idle every worker has zero
reservations, every worker scores identically, and the `worker_id` tie-break
sends everything to `worker-1`. If each job finishes before the next arrives,
reservations return to zero between decisions and the policy becomes "always
worker-1" — measurably *worse* balance than round-robin, which rotates and
cannot degenerate this way.

The behaviour is correct per specification (a deterministic `worker_id`
tie-break is required, and a pure snapshot function is what makes replay work),
but it has a direct consequence for Phase 3:

> **Threat to Phase 3 validity.** Run the adaptive-vs-static comparison at low
> load and Adaptive will beat Static for the wrong reason — because Static
> degenerates under ties, not because telemetry carries information. Phase 3
> comparisons must run where reservations meaningfully differ between decisions,
> and every reported run must state its offered load.

Recorded in `docs/limitations.md`. Mitigations if needed: a spread-aware
tie-break (deterministic and still static, but stateful and therefore not
replayable), or simply reporting the load regime per run.

### 5.5 Static placement under external pressure — Phase 2 claim, corrected

Profile B, loaded, with `worker-1` driven to ~100% CPU by processes outside
Nimbus. Telemetry recorded the pressure throughout; placement could not see it.

> **⚠ The Phase 2 conclusion drawn from this table has been withdrawn.**
> These were **single runs per strategy**. Phase 3 repeated the profile 34 times
> and found makespan ranges **11–54 s for every strategy**, because it is set
> almost entirely by one straggler. Three single draws cannot distinguish
> schedulers in that distribution, and the claim that Static was "73% slower
> than round-robin" was not supported by adequate repetitions.

| Strategy | makespan | thr/s | util CV | observed CPU (w1/w2/w3) |
|---|---|---|---|---|
| `fifo` (round-robin) | 30.57 s | 1.96 | 0.7918 | 97% / 7% / 7% |
| `static_resource_aware` | 52.82 s | 1.14 | 1.2308 | 100% / 6% / 4% |
| `static_resource_aware_binpack` | 45.42 s | 1.32 | 1.2313 | 98% / 6% / 3% |

What survives, and was re-confirmed with repetitions in Phase 3: the divergence
between model and reality is real and large. Nimbus reports the pressured
worker's full declared capacity as free while telemetry reads it at ~100% CPU.

The *mechanism* Phase 2 asserted — "the pressured worker completes fewer jobs,
so it holds the fewest reservations, so a least-loaded policy sends it more" —
also needs qualifying. Phase 3 measurement shows the opposite effect is at least
as strong: slow jobs **hold their slots longer**, so the pressured worker is
frequently at capacity and gets filtered out on feasibility. Both are present.
Which dominates was never measured, and Phase 2 stated one of them with more
confidence than the evidence supported.

What the reservation-only view genuinely lacks is **stability**: measured across
34 runs, `static_resource_aware` sent between 12% and 33% of jobs to the
pressured worker (18.9% ± 8.9%), because reservations churn. adaptive-v1 sent
13.1% ± 1.7%. See §7.7.

## 6. Worker telemetry — collection and quality

**Status: implemented (Phase 2); consumed by `adaptive` from Phase 3.**
`worker/telemetry.py`, `worker/heartbeat.py`.

Through Phase 2 nothing read these samples — that blindness *was* the control
that made the Phase 3 comparison meaningful. adaptive-v1 (§7) is the first and
only policy to consume them, and the static policies remain structurally unable
to (§3).

### 6.1 What is sampled

| Field | Scope | Notes |
|---|---|---|
| `cpu_percent` | worker | Mean over the cores this worker is pinned to |
| `cpu_percent_per_core` | worker | Per-core detail for the same set |
| `process_memory_mb` | worker | RSS of this worker's executor children |
| `active_jobs` | worker | Jobs executing at sample time |
| `memory_percent` | host | Whole machine |
| `available_memory_mb` | host | Whole machine |
| `load_average_1m` | host | Whole machine |
| `sample_duration_ms` | meta | Cost of taking the sample |

Nothing is collected merely because psutil offers it. Disk and network I/O
counters are omitted until a workload class needs them.

### 6.2 Local scoping, and its limits

A local worker is a process pool pinned to a disjoint core set, not its own EC2
instance. So "this worker's CPU" is the mean utilisation of *its cores* — which
is what makes per-worker CPU an independent signal locally, and what lets an
external stressor pinned to those cores show up against the right worker.
Verified: a stressor on `worker-1`'s cores drives its reading to 100% while
`worker-2` and `worker-3` stay near their own load.

Memory has no equivalent local isolation: host RAM is shared, so
`memory_percent` reads identically for all three local workers and cannot
discriminate between them. Both the host-scoped and worker-scoped figures are
recorded and labelled rather than conflated. On EC2 (Phase 5) each worker owns
its host and the host-scoped fields become genuinely per-worker. Recorded in
`docs/limitations.md`.

### 6.3 Sampling

Configurable via `NIMBUS_TELEMETRY_INTERVAL_S` or
`NIMBUS_TELEMETRY_INTERVAL_SECONDS` (both accepted), default **2.0 s**. The
collector waits an interval before its first sample, because
`psutil.cpu_percent(interval=None)` reports utilisation since the previous call
by this process. That also imposes a **sole-caller rule**: two callers would
steal each other's deltas, so `TelemetryCollector` is the only component that
calls it.

### 6.4 Heartbeats are independent

Telemetry ingestion refreshes a worker's heartbeat — a worker reporting metrics
is self-evidently alive — but the converse does not hold. `HeartbeatEmitter`
runs unconditionally, so with telemetry disabled the fleet still proves liveness
and stays schedulable. Without that, the telemetry-OFF control arm would watch
its whole fleet drift to `UNHEALTHY` and place nothing.

A heartbeat carries no resource information, deliberately: liveness is a
correctness signal every strategy needs, while resource observation is the
experimental variable.

### 6.5 ON/OFF control and measured overhead

`PUT /telemetry {"enabled": false}` stops the collector and clears stored
observations, so the OFF arm has none rather than stale ones.

Measured over 3 trials per arm, 40 jobs, 1 s sampling interval:

| Measure | Value |
|---|---|
| Mean sample cost | 0.145 ms per worker |
| Mean sweep cost | 1.338 ms for 3 workers |
| **Duty cycle** | **0.134% of wall time** |
| Sampling errors | 0 |
| Median throughput, telemetry OFF | 8.49 jobs/s (range 8.35–8.57) |
| Median throughput, telemetry ON | 8.35 jobs/s (range 8.18–8.59) |
| Median delta | −1.7% |

The −1.7% delta is **smaller than the within-arm spread** (up to 0.40 jobs/s),
so these runs do not demonstrate a throughput cost — and equally do not prove
there is none. The defensible overhead figure is the duty cycle, which is
directly measured. Reporting the delta as "telemetry is free" would be
overclaiming from n=3.

---

## 7. adaptive-v1 — telemetry-aware placement

**Status: implemented (Phase 3).** `scheduler/placement/adaptive.py`.

Fixed in [`adaptive-v1-preregistration.md`](./adaptive-v1-preregistration.md)
**before** the code existed and before any Phase 3 experiment ran. It must not
change in response to results; an improved algorithm becomes `adaptive-v2` and a
separate experiment.

### 7.1 What it is

Static Resource-Aware **plus** observed host state — never observed state alone.
Nimbus knows about work it has already dispatched that may not yet appear in
telemetry, so the reservation and queue terms are carried over from Phase 2
unchanged.

```
Score(w, j) = α_c · S_cpu + β_c · S_memory + γ_c · S_reservation
            + δ_c · S_queue + ε_c · S_trend
```

All components in `[0, 1]`, weights sum to 1, `Score ∈ [0, 1]`, lower is better,
ties break on `worker_id`.

| Component | Definition | Category |
|---|---|---|
| `S_cpu` | `cpu_percent_smoothed / 100` | OBSERVED |
| `S_memory` | `max(memory_percent/100, process_memory_mb/declared_memory_mb)` | OBSERVED |
| `S_reservation` | `0.5·projected_cpu_res + 0.5·projected_mem_res` | DECLARED + RESERVED |
| `S_queue` | `(slots_used + 1) / slots_total` | DECLARED + RESERVED |
| `S_trend` | `clip(0.5 + slope / (2·20.0))` | OBSERVED |

| Class | α CPU | β Mem | γ Res | δ Queue | ε Trend |
|---|---|---|---|---|---|
| `CPU_BOUND` | **0.50** | 0.10 | 0.20 | 0.15 | 0.05 |
| `MEMORY_BOUND` | 0.10 | **0.50** | 0.20 | 0.15 | 0.05 |
| `IO_BOUND` | 0.15 | 0.15 | 0.25 | **0.40** | 0.05 |
| `LIGHTWEIGHT` | 0.10 | 0.05 | 0.25 | **0.55** | 0.05 |

### 7.2 The blind spot it targets

Phase 2 measured a positive feedback loop:

```
external pressure on W1 → W1 runs slowly → W1 completes fewer jobs
   → W1 holds the fewest reservations → "least loaded" policy reads it as idle
   → more jobs sent to W1 → worse
```

**Low reservation pressure does not imply high execution capacity.** adaptive-v1
can tell the difference because `S_cpu` is *measured* rather than inferred.

Worked example (`tests/unit/test_adaptive_placement.py`):

| | reserved cpu | observed cpu | static score | adaptive score |
|---|---|---|---|---|
| worker-1 | 256 | **99%** | **0.328** ← static picks | 0.677 |
| worker-2 | 1024 | 20% | 0.656 | **0.429** ← adaptive picks |

### 7.3 Smoothing — fixed at K = 3

`cpu_percent_smoothed` is the mean of the last 3 telemetry samples (~6 s at the
default interval), computed by the registry, which owns the history buffer, so
policies stay pure functions of the snapshot they receive. `S_trend` is the
least-squares slope over the same window.

Fixed before experiments. No smoothing-window study runs inside Phase 3.

### 7.4 Telemetry freshness and fallback

```
telemetry_age = decision_time − telemetry_timestamp
NIMBUS_TELEMETRY_MAX_AGE_SECONDS   default 6.0  (3 intervals at 2 s)
```

Telemetry is **stale** if missing, unsampled, or older than the threshold. A
stale worker's *observed* components are replaced by its reservation-derived
equivalents:

```
S_cpu := projected_cpu_res     S_memory := projected_mem_res     S_trend := 0.5
```

This keeps every candidate on one scale (mixing a static score with an adaptive
score would compare incommensurable numbers) and uses Nimbus's best available
estimate.

**Explicitly forbidden, and tested:** treating missing telemetry as 0% CPU.
That would make the least-known worker the most attractive one.

If *every* eligible worker is stale, adaptive-v1 consumes no observed state and
the decision is a pure function of DECLARED + RESERVED. It does not become
bit-identical to `static_resource_aware` — the effective weights differ — but
the inputs are identical.

### 7.5 Hard constraints, and no threshold hacks

```
Workers → static feasibility → eligible → telemetry freshness → adaptive scoring → best
```

Feasibility is evaluated on **static fields only**, by the same
`infeasibility_reason` every policy uses. Telemetry ranks; it never changes
eligibility in either direction.

There is no `if cpu > 90: skip` rule anywhere. Scoring is continuous —
`test_scoring_is_continuous_with_no_threshold_cliff` asserts that 89.9% and
90.1% differ by exactly `α × 0.002`, and that score is monotonic in observed
CPU across the whole range.

### 7.6 Oscillation: measured, not pre-emptively engineered

adaptive-v1 ships with **no** hysteresis, damping or anti-flap logic beyond the
3-sample smoothing window. If reacting to telemetry causes oscillation — job
lands, CPU rises, scheduler flees, CPU falls, scheduler floods — that is a
finding to document before anything is added to suppress it.

Instrumented per run in `benchmark/metrics.py`: `switch_rate`,
`concentration_max` (worst single-worker share inside one telemetry interval —
the direct measure of telemetry-lag herding), plus `tie_rate` and `mean_margin`.

### 7.7 Measured behaviour (Phase 3)

Under Profile B, 34 runs, N=8-9 per strategy:

| Strategy | jobs to pressured worker | makespan median | straggler runs |
|---|---|---|---|
| **`adaptive`** | **13.1% ± 1.7%** | **11.6 s** | **1 / 8** |
| `static_resource_aware` | 18.9% ± 8.9% | 31.0 s | 5 / 9 |
| `fifo` | 26.1% ± 3.3% | 38.2 s | 7 / 9 |
| `static_resource_aware_binpack` | 32.7% ± 5.6% | 46.0 s | 6 / 8 |

Mann-Whitney U on makespan, Bonferroni-corrected: adaptive beats all three
baselines (p = 0.0039 / 0.0053 / 0.0023, Cliff's δ = −0.83 / −0.81 / −0.91).
The pre-registered materiality shortcut nonetheless fails on its
disjoint-ranges requirement; both verdicts are reported in
`docs/experiments.md` §5.7.

**Why it works despite a low CPU weight for I/O jobs.** The stragglers are
`file_scan` and `compress_file` — `IO_BOUND`, where observed CPU carries only
0.15 weight. Steering still happens, through three terms at once: `queue` (0.40)
and `reservation` (0.25) both rise because slow jobs hold slots longer, and
`cpu` (0.15) adds a small but *persistent* penalty the static policy does not
have. The static policy sees only the first two, and those fluctuate as jobs
start and finish — which is why its share swings 12–33% while adaptive's holds
at 12–17%.

That was not predicted. The pre-registration reasoned that I/O-bound jobs are
mostly blocked so host CPU matters little — true, but on a CPU-saturated host
they suffer anyway, and the small constant penalty is what makes the avoidance
*stable*. Whether a higher `IO_BOUND` CPU weight helps further is an
`adaptive-v2` question and was deliberately not tried.

**No oscillation was observed.** Measured across the same 34 runs:

| Strategy | switch rate | concentration max | **tie rate** |
|---|---|---|---|
| `adaptive` | 0.56 [0.53–0.61] | 0.86 [0.80–1.00] | **0.01** |
| `static_resource_aware` | 0.52 [0.47–0.58] | 0.89 [0.83–1.00] | **0.35** |
| `fifo` | 0.78 [0.69–0.90] | 0.72 [0.56–0.89] | 0.00 |
| `static_resource_aware_binpack` | 0.44 [0.32–0.53] | 1.00 | **0.36** |

adaptive-v1 sits between the static policies and round-robin on switching, and
its placement share was the least variable of the four. The 3-sample smoothing
window was sufficient at a 1 s telemetry interval; no damping was needed, and
none was added.

**A confound this exposed, which must be reported.** The static policies decided
**35–36% of placements by the `worker_id` tie-break**, not by their own scoring
— R9 is active in the *loaded* regime, not just at low load as Phase 2 assumed
when the load gate was written. adaptive-v1 ties on 1% of decisions, because a
continuous observed signal rarely produces exact equality.

So the adaptive-vs-`static_resource_aware` comparison is partly confounded: some
of adaptive's advantage is "not tying" rather than telemetry content. That
distinction may be academic — eliminating arbitrary tie-breaks is a real benefit
of having a continuous signal — but it is not what H3 claimed to test.

**`fifo` never ties** (rotation offsets are distinct integers), so
**adaptive vs `fifo` is the clean comparison**, and adaptive wins it
(−69.5%, p = 0.0053, Cliff's δ = −0.81).

---

## 8. The decision record

**Status: implemented (Phase 2).** Persisted for **every** placement attempt,
including ones that placed nothing.

Every worker appears in `candidates` with an `eligible` flag — ineligible
workers are recorded, not omitted — and each carries all three state categories.
`observed` is present and was **not** an input to `score`.

```json
{
  "decision_id": "dec-a5f2ebacaaa1",
  "job_id": "job-e45f9cc8bfda",
  "attempt": 1,
  "strategy": "static_resource_aware",
  "ordering_policy": "fifo",
  "placement_policy": "static_resource_aware",
  "workload_class": "CPU_BOUND",
  "priority": 3,
  "effective_priority": 3,
  "job": {
    "job_type": "prime_count", "workload_class": "CPU_BOUND",
    "priority": 3, "cpu_units": 1024, "memory_mb": 64, "attempts": 0
  },
  "decided_at": "2026-08-21T15:53:56.870992Z",
  "decision_latency_us": 20.265,
  "fleet_snapshot_at": "2026-08-21T15:53:56.872966Z",
  "telemetry_enabled": true,
  "weights": { "cpu": 0.40, "memory": 0.30, "slots": 0.30 },
  "candidates": {
    "worker-1": {
      "eligible": false,
      "ineligible_reason": "no free slot: 2/2",
      "healthy": true,

      "declared_cpu_units": 2048, "declared_memory_mb": 4096, "declared_slots": 2,
      "reserved_cpu_units": 2048, "reserved_memory_mb": 128, "active_jobs": 2,

      "observed": {
        "worker_id": "worker-1",
        "timestamp": "2026-08-21T15:53:56.696133Z",
        "cpu_percent": 100.0,
        "process_memory_mb": 80.49,
        "active_jobs": 2,
        "memory_percent": 77.5,
        "available_memory_mb": 3693.5,
        "load_average_1m": 5.28,
        "sample_duration_ms": 0.2378
      },
      "observed_age_s": 0.1765,

      "score": null,
      "terms": {}
    },
    "worker-3": {
      "eligible": true, "ineligible_reason": null, "healthy": true,
      "declared_cpu_units": 2048, "declared_memory_mb": 4096, "declared_slots": 2,
      "reserved_cpu_units": 1024, "reserved_memory_mb": 64, "active_jobs": 1,
      "observed": { "cpu_percent": 29.25, "...": "..." },
      "score": 0.7093750000000001,
      "terms": {
        "projected_cpu": 1.0, "projected_memory": 0.03125, "projected_slots": 1.0,
        "contrib_cpu": 0.4, "contrib_memory": 0.009375, "contrib_slots": 0.3
      }
    }
  },
  "excluded": {
    "worker-1": "no free slot: 2/2",
    "worker-2": "no free slot: 2/2"
  },
  "selected_worker": "worker-3",
  "runner_up": null,
  "margin": null,
  "reason": "lowest static resource score (balance): worker-3=0.7094 [cpu 1024+1024/2048, mem 64+64/4096, slots 1+1/2]; 1 eligible, 2 excluded"
}
```

Field notes:

- **`attempt`** is the attempt this decision authorises. The engine increments
  `Job.attempts` on `QUEUED → SCHEDULED`, so recording the pre-increment value
  would label a first placement "attempt 0".
- **`healthy`** is recorded per candidate so a trace is self-describing and
  exclusions replay exactly.
- **`observed.active_jobs`** may disagree with `active_jobs` (RESERVED): the
  former is what the worker agent reported at its last sample, the latter is
  what the control plane has committed *now*. The gap is telemetry staleness,
  and it is visible rather than smoothed away — Phase 3's anti-herding
  correction (§7.4) exists precisely because of it.
- **`terms`** for `static_resource_aware` contains only `projected_*` values,
  all derived from DECLARED and RESERVED. There is no observed quantity in the
  scoring path.
- **`margin`** (runner-up minus selected) is the confidence signal. A policy
  whose margins are consistently near zero is not discriminating — worth
  reporting.

### 8.1 Replay

`scheduler/replay.py` rebuilds a decision's inputs from the trace and
re-executes the policy:

```python
from scheduler.replay import verify, verify_all
verify(decision).matches          # recorded worker and every score reproduced
matched, mismatched = verify_all(decisions)
```

Only scored policies are replayable; `round_robin` carries a rotation cursor and
raises `NotReplayable` rather than guessing. Replay is how the telemetry
boundary is verified end to end, and it is what will let Phase 3 re-score
archived traces counterfactually without re-running the workload.

## 9. What is deliberately not implemented

Recorded so the report can state its own scope honestly:

- **Preemption.** No running job is ever killed to make room.
- **Migration.** Placement is final once execution starts.
- **Gang / multi-node scheduling.** One job, one worker.
- **DAG dependencies.** AWS Batch `dependsOn` has no Nimbus equivalent.
- **Backfill.** No lookahead past the head of the ordering.
- **Cross-tenant fairness.** Batch fair-share and SQS fair queues have no Nimbus
  equivalent; only single-queue starvation control (§4, Scheduler B) exists.
- **Autoscaling.** Fixed fleet, by experimental design.
- **ML.** Explicitly out of scope until the deterministic baseline is measured.
