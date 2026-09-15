# adaptive-v1 — Pre-Registration

**Written 2026-08-22, before any adaptive code was implemented and before any
Phase 3 experiment was run.** Nothing in this document may be changed in
response to results. If adaptive-v1 performs badly, the result stands and any
improved algorithm becomes a separately labelled `adaptive-v2`.

---

## 1. Hypotheses

**H3 (primary).** Under loaded heterogeneous workloads with externally induced
worker pressure, telemetry-aware placement will reduce makespan and
worker-utilisation imbalance relative to Static Resource-Aware placement,
because observed utilisation exposes capacity degradation that is invisible to
the reservation model.

**H3-null.** Telemetry-aware placement does not materially improve scheduling
outcomes relative to Static Resource-Aware placement.

**H3-secondary.** Under what workload regimes does telemetry help, have
negligible effect, or hurt?

### Success criterion

Success is **not** "Adaptive wins every metric". Success is:

> Adaptive provides a *repeatable* benefit specifically in regimes where
> OBSERVED host state contains information unavailable to Static placement, and
> does not materially harm regimes where it does not.

Adaptive must be judged against the **best relevant baseline**, not the easiest
one. Under Profile B in Phase 2, FIFO round-robin beat Static Resource-Aware, so
**FIFO is the bar for Profile B**, not Static.

### Materiality threshold, fixed in advance

A difference in a primary metric counts as **material** only if:

1. the median difference across repetitions is ≥ **10%** relative, **and**
2. the two arms' per-run ranges do not overlap.

Anything smaller is reported as **"no meaningful observed difference"**,
regardless of its sign.

---

## 2. The adaptive-v1 formula

For an eligible worker `w` and job `j` of workload class `c`:

```
Score(w, j) = α_c · S_cpu(w)
            + β_c · S_memory(w)
            + γ_c · S_reservation(w, j)
            + δ_c · S_queue(w)
            + ε_c · S_trend(w)
```

All five components are normalised to `[0, 1]`, every weight vector sums to 1,
so `Score ∈ [0, 1]`. **Lower is better.** Ties break on the lexicographically
smallest `worker_id`, exactly as in Phase 2.

### 2.1 Components

| Component | Definition | Category |
|---|---|---|
| `S_cpu` | `clip(cpu_percent_smoothed / 100, 0, 1)` | OBSERVED |
| `S_memory` | `clip(max(memory_percent / 100, process_memory_mb / declared_memory_mb), 0, 1)` | OBSERVED |
| `S_reservation` | `0.5 · projected_cpu_res + 0.5 · projected_mem_res` | DECLARED + RESERVED |
| `S_queue` | `(slots_used + 1) / slots_total` | DECLARED + RESERVED |
| `S_trend` | `clip(0.5 + slope / (2 · TREND_SCALE), 0, 1)` | OBSERVED |

where, reusing the Phase 2 static projections verbatim:

```
projected_cpu_res = (cpu_reserved    + j.cpu_units) / cpu_capacity
projected_mem_res = (memory_reserved + j.memory_mb) / memory_capacity
```

`S_reservation` and `S_queue` are **exactly the static model**. adaptive-v1 is
Static plus observed information, never observed information alone — Nimbus
knows about work it has already dispatched that may not yet appear in telemetry,
and discarding that would be strictly worse.

### 2.2 Memory signal — and its known local weakness

`S_memory` takes the **maximum** of a host-scoped and a worker-scoped reading,
because both answer "how close is this worker to memory exhaustion" and the
larger is the conservative answer:

- host-scoped `memory_percent` — correct on EC2, where a worker owns its host;
- worker-scoped `process_memory_mb / declared_memory_mb` — RSS of this worker's
  own executor children.

**Stated in advance:** on a local fleet, three workers share one machine's RAM,
so `memory_percent` is *identical* for all of them and cannot discriminate. An
external memory stressor raises it equally everywhere. Therefore **Profile C
cannot demonstrate memory discrimination locally**, and a null result there is
expected for a measurement reason, not an algorithmic one. It is run anyway, and
reported with that caveat rather than omitted.

### 2.3 CPU smoothing — fixed at K = 3

`cpu_percent_smoothed` is the arithmetic mean of the **last 3 telemetry
samples** (fewer if fewer exist). At the default 2 s interval that is a ~6 s
window.

Fixed now, before experiments. No smoothing-window study will be run inside
Phase 3; if one is wanted later it becomes a separate labelled experiment.

### 2.4 Trend

`slope` is the least-squares slope of CPU% against sample timestamp over the
same 3-sample window, in percent per second.

`TREND_SCALE = 20.0` %/s. A flat trend gives `S_trend = 0.5` (neutral), rising
tends to 1 (worse), falling tends to 0 (better). With fewer than 2 samples,
`slope = 0` and `S_trend = 0.5`.

### 2.5 Weight vectors — frozen

| Class | α CPU | β Memory | γ Reservation | δ Queue | ε Trend |
|---|---|---|---|---|---|
| `CPU_BOUND` | **0.50** | 0.10 | 0.20 | 0.15 | 0.05 |
| `MEMORY_BOUND` | 0.10 | **0.50** | 0.20 | 0.15 | 0.05 |
| `IO_BOUND` | 0.15 | 0.15 | 0.25 | **0.40** | 0.05 |
| `LIGHTWEIGHT` | 0.10 | 0.05 | 0.25 | **0.55** | 0.05 |

Rationale: CPU-bound runtime is dominated by core contention; memory-bound
failure is exhaustion, which is catastrophic rather than gradual; I/O-bound jobs
spend most of their life blocked, so concurrency matters more than host CPU;
lightweight jobs are milliseconds long, so queueing delay is essentially the
whole latency and telemetry noise should not drive placement.

---

## 3. Telemetry freshness and fallback

```
telemetry_age = decision_time − telemetry_timestamp
NIMBUS_TELEMETRY_MAX_AGE_SECONDS, default 6.0   (3 sampling intervals at 2 s)
```

A worker's telemetry is **stale** if it is missing entirely, or older than the
threshold, or backed by zero samples.

**Fallback rule (per worker, deterministic):** a stale worker's *observed*
components are replaced by its reservation-derived equivalents:

```
S_cpu    := projected_cpu_res
S_memory := projected_mem_res
S_trend  := 0.5              (neutral)
```

Rationale: this keeps every candidate on one `[0, 1]` scale (mixing a static
score with an adaptive score would compare incommensurable numbers), it never
assumes an unobserved worker is idle, and it uses Nimbus's best available
estimate of that worker's load.

**Consequence, stated in advance:** if *every* eligible worker is stale,
adaptive-v1 consumes no observed state at all and the decision is a pure
function of DECLARED + RESERVED. It does **not** become bit-identical to
`static_resource_aware`, because the effective weights differ; it becomes a
static decision under adaptive-v1's weights. This is intentional and is tested.

**Explicitly forbidden:** treating missing telemetry as 0% CPU. That would make
an unobserved worker maximally attractive and is the exact failure mode §10 of
the brief warns about.

---

## 4. Hard constraints, and no threshold hacks

Feasibility is unchanged from Phase 2 and is evaluated on **static fields
only**:

```
healthy  →  free slot  →  sufficient declared CPU  →  sufficient declared memory
```

Telemetry influences **ranking only**. It can never make an infeasible worker
eligible, and it can never make a feasible worker ineligible. There is no
`if cpu > 90: skip` rule anywhere in adaptive-v1 — scoring is continuous, so
89.9% and 90.1% differ by 0.2% of a weighted term rather than by
eligible/ineligible.

Pipeline:

```
Workers → static feasibility → eligible → telemetry freshness → adaptive scoring → best
```

---

## 5. Oscillation: measured, not pre-emptively engineered

adaptive-v1 contains **no** hysteresis, damping, PID control or anti-flap logic
beyond the pre-declared 3-sample smoothing window. If placement oscillates, that
is a finding to document, not something to suppress before it is observed.

Instrumented per run:

- **`placement_switch_rate`** — fraction of consecutive placements landing on a
  different worker than the previous placement.
- **`placement_concentration`** — the maximum share of any single worker across
  placements made within one telemetry interval. This is the direct measure of
  telemetry-lag herding.
- **score evolution** — every decision trace already carries per-component
  scores for every candidate.

---

## 6. Experimental protocol

### 6.1 Held constant between arms

Ordering policy (`fifo`), job sequence (same seed, same generator), arrival
schedule, worker declarations, slot counts, CPU affinity, workload parameters,
pressure profile and backend, retry behaviour, queue implementation, dispatch
mechanism, telemetry collection (on in all arms).

**The only variable is the information available to placement.**

### 6.2 Repetitions — fixed at 5

**N = 5** repetitions per (profile × scheduler), decided before any comparative
result was inspected. Arms are run **interleaved** (round-robin over schedulers
within each repetition) so machine drift cannot systematically favour whichever
arm runs last.

Reported for every cell: **median, full range, and every individual run value.**
Never the best run.

### 6.3 Load regime — the R9 guard

Phase 2 found that Static degenerates at low load: an idle fleet produces score
ties and the `worker_id` tie-break concentrates everything on `worker-1`. A
low-load comparison would show Adaptive winning for a reason unrelated to
telemetry.

**Primary inferential runs must satisfy an offered-load criterion, checked and
reported per run:**

```
mean fleet utilisation over the makespan window ≥ 0.50
```

computed as `mean(util(w))` from the existing `benchmark/metrics.py`
definition. Runs below this threshold are reported as **boundary-condition /
descriptive** results and are **not** used as evidence for or against H3.

### 6.4 Profiles

| Profile | Purpose | Role |
|---|---|---|
| A `homogeneous_saturated` | No external pressure. Does Adaptive add overhead or instability when telemetry carries little extra information? | Primary — a null result here is a *good* result |
| B `cpu_pressure` | External CPU load on `worker-1`, invisible to the reservation model | **Primary experiment** |
| C `memory_pressure` | External memory consumption | Primary, with the §2.2 caveat |
| D `heavy_tail` | Pareto runtimes; reservations poorly represent completion time | Secondary |
| E `burst` | Step change in offered load | Secondary |
| F dynamic pressure | Pressure moves between workers mid-run | Optional, only after A–E |

### 6.5 Primary metrics

Makespan, throughput, completion p50, completion p95, queue wait, worker busy
time, utilisation CV, scheduler decision latency. Also recorded: placement
distribution, observed resource state, telemetry freshness, switch rate,
concentration.

### 6.6 Investigating a large win

Any result where Adaptive beats a baseline by more than **2×** on a primary
metric is treated as a suspected experimental bug until each of the following is
verified and reported: identical seed and job plan, identical pressure spec and
backend, identical CPU affinity, identical slot counts and declared capacity,
identical ordering policy, identical queue and retry configuration, identical
worker health throughout, and comparable telemetry overhead.

---

## 7. Algorithm versioning

Every decision trace, experiment manifest and benchmark result records
`algorithm_version`. adaptive-v1 is `adaptive-v1`. Existing policies are
retro-labelled `round-robin-v1` and `static-v1`.

Version identities are never overwritten. A changed formula, changed weights,
changed smoothing window or changed freshness policy is a **new version** and a
new experiment.


---

## 8. Post-hoc addenda (added after experiments ran — disclosed, not merged above)

Nothing in sections 1–7 was changed after results were seen. This section
records what had to be added, and why, so the pre-registration and its
shortcomings stay separable.

| Added | When | Why |
|---|---|---|
| `tie_rate`, `mean_margin` metrics | **before** any Phase 3 run | The §6.3 load gate is only a *proxy* for the R9 confound; these measure it directly. Timing disclosed because they were not in the original protocol. |
| Straggler-rate analysis (longest job > 15 s) | after | Profile B makespan turned out bimodal; this describes the structure. A descriptive cut, not a test. |
| Mann-Whitney U + Cliff's δ on makespan | after | Follows `experiments.md` §2.3, the project's standing protocol from Phase 1 — not a criterion invented to fit the answer. |
| N raised from 5 to 8–9 for Profile B | after | Makespan variance turned out far larger than anticipated. More data, same protocol. |

### What the pre-registration got wrong

1. **The materiality criterion (§1) is unusable on a bimodal distribution.**
   Requiring non-overlapping per-run ranges is near-unsatisfiable at N≈9 no
   matter how large the effect: one unlucky run in either arm destroys it. It is
   reported as failing rather than edited. `adaptive-v2` should specify a
   rank-based test with an effect-size floor.

2. **The load-regime gate (§6.3) does not guard what it was meant to guard.**
   It assumed R9 tie-break degeneracy was a low-load phenomenon. Measurement
   showed the static policies tie on 35–36% of placements at 0.25–0.85 fleet
   utilisation. The gate also mis-fires on Profile B, where mean utilisation
   *falls* as a consequence of the effect being studied.

3. **Profile C cannot test what it was meant to test**, exactly as §2.2 warned —
   but §6.4 still listed it as a primary profile. It should have been secondary
   from the start.

4. **The `IO_BOUND` reasoning was incomplete.** §2.5 argued I/O-bound jobs care
   little about host CPU. True in isolation — but on a CPU-saturated host they
   become the stragglers that set the makespan. adaptive-v1 steers them away
   anyway, via the queue and reservation terms plus a small persistent CPU
   penalty, which was not the predicted mechanism.
