# Nimbus Architecture

**Status:** design, reviewed before Phase 1 implementation.
Read [`aws-native-scheduling.md`](./aws-native-scheduling.md) first — it defines
what is *not* novel here.

---

## 1. System overview

```
                                  CLIENT
                             (CLI / dashboard)
                                     |
                                     v
                        ┌────────────────────────┐
                        │       REST API         │   FastAPI
                        │  /jobs /workers ...    │
                        └───────────┬────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     v                             v
             ┌───────────────┐            ┌────────────────┐
             │  StateStore   │            │   JobQueue     │
             │  job metadata │            │  pending jobs  │
             │  decisions    │            │  lease / ack   │
             │  telemetry    │            │  nack / DLQ    │
             └───────┬───────┘            └────────┬───────┘
                     │                             │
                     │        ┌────────────────────┘
                     │        │  receive(batch)
                     v        v
             ┌──────────────────────────────────────────┐
             │          SCHEDULER LOOP (singleton)      │
             │  1. receive leased job(s)                │
             │  2. snapshot WorkerRegistry telemetry    │
             │  3. filter unhealthy / over-capacity     │
             │  4. score candidates  (strategy A–D)     │
             │  5. persist SchedulingDecision           │
             │  6. dispatch to chosen worker            │
             └──────────────────┬───────────────────────┘
                                │ push (HTTP / local call)
              ┌─────────────────┼─────────────────┐
              v                 v                 v
        ┌───────────┐     ┌───────────┐     ┌───────────┐
        │ WORKER 1  │     │ WORKER 2  │     │ WORKER 3  │
        │ executor  │     │ executor  │     │ executor  │
        │ telemetry │     │ telemetry │     │ telemetry │
        │ heartbeat │     │ heartbeat │     │ heartbeat │
        └─────┬─────┘     └─────┬─────┘     └─────┬─────┘
              │  telemetry + heartbeat + result   │
              └─────────────────┼─────────────────┘
                                v
                        ┌───────────────┐   ┌──────────────┐
                        │ ArtifactStore │   │  CloudWatch  │
                        │ inputs/outputs│   │ metrics/logs │
                        └───────────────┘   └──────────────┘
```

Three workers, fixed. Fleet size is an experimental control, not a feature —
see `aws-native-scheduling.md` §4.

---

## 2. Control plane / data plane split

The split is enforced by module boundaries and by a rule: **control-plane code
never executes user workload; data-plane code never makes a placement decision.**

### Control plane — `backend/`, `scheduler/`

| Responsibility | Module |
|---|---|
| Job submission, validation, classification | `backend/api/jobs.py`, `backend/services/job_manager.py` |
| Job metadata + state transitions | `backend/services/job_manager.py`, `backend/models/job.py` |
| Queue management (enqueue, lease, ack, nack, DLQ) | `backend/services/queue/*` |
| Job ordering | `scheduler/ordering.py` |
| Placement decision | `scheduler/placement/*` (round_robin, static_resource_aware, adaptive) |
| Algorithm versioning | `PlacementPolicy.algorithm_version`, recorded in every trace and manifest |
| Decision trace assembly | `scheduler/strategy.py` |
| Decision replay / verification | `scheduler/replay.py` |
| Worker discovery + registration | `backend/services/registry.py` |
| Telemetry collection (psutil) | `worker/telemetry.py` |
| Liveness heartbeats | `worker/heartbeat.py` |
| Telemetry ingestion + aggregation | `backend/services/registry.py` |
| Failure detection (heartbeat expiry, job lease expiry) | `backend/services/reaper.py` |
| Retry policy | `backend/services/job_manager.py` |
| Benchmark orchestration | `benchmark/runner.py` |
| Workload profiles / generation | `benchmark/profiles.py`, `benchmark/generator.py` |
| External host pressure | `benchmark/pressure.py` |
| Metrics / manifests | `benchmark/metrics.py`, `benchmark/manifest.py` |

### Data plane — `worker/`

| Responsibility | Module |
|---|---|
| Receiving a dispatched job | `worker/agent.py` |
| Executing the workload | `worker/executor.py` |
| CPU / memory / IO / lightweight workloads | `worker/workloads/*` |
| Reading inputs, writing outputs | `worker/io.py` (via `ArtifactStore`) |
| Reporting telemetry | `worker/telemetry.py` |
| Reporting liveness | `worker/heartbeat.py` |

The worker never chooses what to run. It accepts, executes, reports.

### Why this matters experimentally

If workers could pull work themselves, the placement decision would be made by
whichever worker polled first — i.e. by luck and poll timing — and every
scheduling strategy would degrade to the same "random-ish least-busy" behaviour.
The measurement would be meaningless. This is the constraint the brief flags in
§12, and it drives the queue design below.

---

### 2.1 DECLARED / RESERVED / OBSERVED (Phase 2)

Worker state is split into three categories that are never conflated, because
the project's central experiment depends on telling them apart:

| Category | Type | Source | Mutated by | Readable by placement |
|---|---|---|---|---|
| **DECLARED** | `DeclaredCapacity` | operator configuration | registration only | yes |
| **RESERVED** | `ReservedResources` | Nimbus bookkeeping | `reserve()` / `release()` | yes |
| **OBSERVED** | `WorkerTelemetry` + `ObservedWindow` | psutil | telemetry collector | **`adaptive` only** |

`WorkerState` holds all three and is what the API returns. A placement policy
never receives a `WorkerState`: it declares its information budget through
`build_views()`.

| Policy | View | Sees |
|---|---|---|
| `round_robin`, `static_resource_aware` | `StaticWorkerView` | DECLARED + RESERVED |
| `adaptive` (adaptive-v1) | `AdaptiveWorkerView` | DECLARED + RESERVED + OBSERVED + telemetry quality |

Phase 3 added the second row without weakening the first. `StaticWorkerView`
still has no telemetry fields; both field sets are pinned by tests, and the
static view is asserted to contain none of the observed ones.

`ObservedWindow` is the smoothed summary (mean CPU and least-squares trend over
the last N samples) that adaptive-v1 actually scores on. It is computed by the
registry, which owns the history buffer, so placement policies remain pure
functions of the snapshot they are handed.

The decision trace records all three categories for every candidate. Recording
happens in `SchedulingStrategy._trace`, *after* the policy has returned — so
"telemetry was recorded" and "telemetry was consumed" are different code paths,
and `scheduler/replay.py` verifies the distinction by re-executing decisions
from the static fields alone.

**Heartbeats are separate from telemetry.** Liveness must work with telemetry
disabled, or the ON/OFF control arm would have no healthy fleet. Telemetry
ingestion refreshes a heartbeat; heartbeats carry no resource information.

---

## 3. Queue and dispatch design

**This is the load-bearing architectural decision. It is written down because
getting it wrong silently invalidates every benchmark.**

### 3.1 The problem

SQS is a pull-based queue. The natural pattern — N workers long-polling one queue —
is *competing consumers*: each message goes to whichever worker asked for it. That
pattern **cannot** implement telemetry-aware placement, because no component ever
chooses a worker.

### 3.2 Rejected: per-worker queues

Scheduler decides, then sends the job to `nimbus-worker-{id}`; that worker is the
only consumer of its own queue.

- ✅ Per-worker lease semantics come free from SQS.
- ❌ A dead worker's queue is a black hole. Messages return to visibility inside a
  queue nobody is polling; recovery needs a separate reaper that drains the dead
  worker's queue back to the pending queue — an extra failure-prone path.
- ❌ Placement becomes irrevocable at enqueue time. If a worker degrades between
  decision and pickup, the job cannot be re-placed.
- ❌ Fleet size is baked into infrastructure (N queues in Terraform).

### 3.3 Chosen: scheduler-mediated lease with push dispatch

```
submit ──> [ pending queue ]  ── the SCHEDULER is the ONLY consumer ──┐
                  ^                                                   │
                  │ nack (visibility -> 0)                            v
                  │                                          receive(): lease held
                  │                                          by the SCHEDULER for
                  │                                          the whole job lifetime
                  │                                                   │
                  │                                    score + choose worker
                  │                                                   │
                  │                                    push dispatch (HTTP POST
                  │                                    /execute, or in-process
                  │                                    call in local mode)
                  │                                                   │
                  │                                              WORKER runs it
                  │                                                   │
                  ├── worker crash / job timeout / NACK ◄─────────────┤
                  │                                                   │
                  └── ack (delete) ◄──── COMPLETED ◄──────────────────┘

     receive_count > max_attempts  ──>  [ dead-letter queue ]
```

Rules:

1. **The scheduler is the sole consumer of the pending queue.** In Phase 5 this is
   one process; if it is ever replicated, a DynamoDB-conditional-write lease
   elects a single active scheduler. Correctness does not depend on that: two
   schedulers would produce duplicate placements, which idempotency (§8) absorbs
   at the cost of wasted work.
2. **The scheduler holds the message lease for the entire job lifetime** and
   extends visibility on a timer (`ChangeMessageVisibility`), exactly as the SQS
   docs recommend for long tasks.
3. **Dispatch is a push.** The worker exposes `POST /execute`; it accepts or
   rejects (`409` if at capacity). A rejection is a *fast* signal — the scheduler
   immediately re-scores excluding that worker, without any queue round-trip.
4. **Failure paths all converge on nack.** Worker heartbeat expiry, job execution
   timeout, and worker-reported error each mark the attempt failed and nack the
   message (visibility → 0), returning it to the pending queue for re-placement.
   `receive_count` increments naturally.
5. **Deferral is not failure, and must not use the nack path.** When the
   scheduler receives a job and decides not to place it *right now* — no
   eligible worker, or the worker rejected the dispatch — the job has not
   failed and no attempt was consumed. Releasing the lease would still
   increment `ApproximateReceiveCount`, because SQS counts *deliveries*, not
   *attempts*, and there is no API to decrement it. A burst of jobs that
   momentarily exceeds fleet capacity would therefore walk perfectly healthy
   jobs into the DLQ. Deferral instead **deletes the message and re-sends it
   with a short delay**, resetting the delivery count.

   *(This was found by running Phase 1, not by reading the docs: a 24-job
   heterogeneous batch dead-lettered 5 healthy jobs, with 65 deliveries for 24
   jobs. `Job.attempts` is the authoritative retry counter; the DLQ is a
   poison-message backstop, and its threshold sits above the retry budget for
   the same reason.)*
6. **DLQ on `receive_count > max_attempts + buffer`**, with terminal job status
   `FAILED`.

Trade-offs, stated honestly:

- ✅ Placement is revocable until execution actually starts.
- ✅ One queue; fleet size is a runtime concern, not an infrastructure one.
- ✅ Failure recovery is one path, exercised identically by every failure mode.
- ❌ The scheduler is a throughput bottleneck and a single point of failure. For
  this experiment that is *acceptable and in fact desirable*: a centralised
  decision point is what makes strategies comparable. It is recorded in
  `docs/limitations.md`.
- ❌ Scheduler↔worker connectivity is now on the critical path (security group
  rules, private subnet routing).

#### Transport: in-process or HTTP

`NIMBUS_WORKER_TRANSPORT` selects what the engine's `_workers` hold; the
scheduler, reaper and heartbeat code do not know which. `factory.build_workers`
returns either `LocalWorkerAgent`s (`inprocess`, the default) or one
`RemoteWorkerClient` per entry of `NIMBUS_WORKER_ENDPOINTS`
(`"worker-1=http://host:8080,..."`; the ids become the registered worker ids).

- **Dispatch.** `try_execute` is `POST /execute` with the `Job` as JSON, 1 s
  connect timeout and `NIMBUS_DISPATCH_TIMEOUT_S` read timeout. A `202` whose
  body names the expected `worker_id` is acceptance; `409` is "at capacity".
  Both a `409` and any dispatch error (5xx, connection error, timeout, reply from
  the wrong worker) return `False`, which takes the existing rejection path:
  re-queue by deferral, no retry budget spent (rule 5 above).
- **Ambiguous read timeout.** A timeout on `/execute` may mean the worker
  accepted the job. The job is re-dispatched with the *same* `execution_id`; the
  agent's dedupe and the engine's execution-id guard (§8, guard 3) keep
  completion exactly-once. This relies on that dedupe, not on the transport.
- **Liveness.** `alive()` is `GET /health`: true only for a `200` that is ok, not
  `crashed`, and carries the expected `worker_id`. The heartbeat emitter beats a
  worker only when it is not simulated-failed *and* `alive()`; a worker whose
  `/health` fails ages into `UNHEALTHY` exactly as a killed local one does.
- **Telemetry.** `RemoteTelemetryCollector` polls `GET /telemetry` on the
  telemetry interval and ingests successes only. It skips workers simulated as
  failed, because `ingest_telemetry` also refreshes the heartbeat.
- **Results.** The worker's `ResultReporter` POSTs the result to the control
  plane's `/internal/results`, retrying; the router is mounted when
  `backend == aws` *or* `worker_transport == http`. Duplicate and superseded
  results are discarded by the same guards as local completions.
- **Abandon.** `abandon(execution_id)` is `POST /abandon`, fire-and-forget on a
  one-thread executor, so a dead worker cannot stall the reaper inside `tick()`.
  It returns once the call is queued, not once the worker has acted.
- **Failure simulation.** `fail_worker(hard=False)` (a partition) works on remote
  workers; `hard=True` raises `NotImplementedError` -- a remote host's pool is
  not ours to crash.

Built and tested locally (`MockTransport` bridges into the real FastAPI apps,
`tests/integration/test_http_dataplane.py`); **never run across a real network.**

### 3.4 The `JobQueue` interface

Modelled directly on SQS semantics so that local behaviour transfers:

```python
class JobQueue(Protocol):
    def enqueue(self, job_id: str, *, delay_s: float = 0) -> None: ...
    def receive(self, max_messages: int = 1, wait_s: float = 0) -> list[Lease]: ...
    def extend(self, lease: Lease, visibility_s: float) -> None: ...
    def ack(self, lease: Lease) -> None: ...
    def nack(self, lease: Lease) -> None: ...          # visibility -> 0
    def depth(self) -> QueueDepth: ...                 # visible / in-flight
```

```python
@dataclass(frozen=True)
class Lease:
    job_id: str
    receipt: str          # SQS ReceiptHandle, or a local token
    receive_count: int    # SQS ApproximateReceiveCount
```

Implementations: `LocalJobQueue` (in-process, thread-safe, real visibility timers,
real DLQ) → `SQSJobQueue`.

---

## 4. Job state machine

```
                      submit
                        │
                        v
                  ┌──────────┐
        ┌────────►│  QUEUED  │◄──────────────┐
        │         └────┬─────┘               │
        │              │ scheduler leases    │
        │              │ + selects worker    │
        │              v                     │
        │         ┌───────────┐              │
        │         │ SCHEDULED │              │ nack, attempts < max
        │         └────┬──────┘              │
        │              │ worker ACCEPTs      │
        │              │                     │
        │   dispatch   │                     │
        │   rejected/  v                     │
        │   failed ┌──────────┐              │
        └──────────┤ RUNNING  ├──────────────┤ heartbeat expiry
                   └────┬─────┘              │ / exec error
                        │                    │
          ┌─────────────┼──────────────┐     │
          │             │              │     │
     exit 0        exec error     wall-clock │
          │             │           timeout  │
          v             v              v     │
    ┌───────────┐   ┌────────┐  ┌───────────┐│
    │ COMPLETED │   │RETRYING├──┘ TIMED_OUT ├┘
    └───────────┘   └───┬────┘  └─────┬─────┘
                        │             │
             attempts >= max_attempts │
                        └──────┬──────┘
                               v
                          ┌────────┐
                          │ FAILED │  (message also in DLQ)
                          └────────┘
```

`RETRYING` and `TIMED_OUT` are **transient bookkeeping states**, entered for the
duration of one transition so that the dashboard and the decision log can show
*why* a job went back to `QUEUED`. Terminal states: `COMPLETED`, `FAILED`.

Transition table (the authoritative version lives in `backend/models/job.py` and
is unit-tested):

| From | Event | To |
|---|---|---|
| — | `submit` | `QUEUED` |
| `QUEUED` | `schedule(worker)` | `SCHEDULED` |
| `SCHEDULED` | `accepted` | `RUNNING` |
| `SCHEDULED` | `dispatch_rejected` | `QUEUED` |
| `SCHEDULED` | `worker_lost` | `RETRYING` |
| `RUNNING` | `completed` | `COMPLETED` |
| `RUNNING` | `failed` | `RETRYING` |
| `RUNNING` | `timeout` | `TIMED_OUT` |
| `RUNNING` | `worker_lost` | `RETRYING` |
| `RETRYING` / `TIMED_OUT` | `attempts < max` | `QUEUED` |
| `RETRYING` / `TIMED_OUT` | `attempts >= max` | `FAILED` |

Every other (state, event) pair raises `IllegalTransition`. Tested exhaustively.

---

## 5. Worker state machine

```
   register
      │
      v
 ┌──────────┐  heartbeat age > timeout   ┌───────────┐
 │ HEALTHY  ├──────────────────────────► │ UNHEALTHY │
 │          │◄────────────────────────── │           │
 └────┬─────┘   heartbeat received       └─────┬─────┘
      │         (age <= timeout)               │
      │ drain requested                        │ age > eviction_timeout
      v                                        v
 ┌──────────┐                            ┌──────────┐
 │ DRAINING │───── active_jobs == 0 ────►│   DEAD   │
 └──────────┘                            └──────────┘
```

- `HEALTHY` — eligible for placement.
- `UNHEALTHY` — heartbeat older than `heartbeat_timeout_s` (default 15 s, i.e.
  3 missed 5 s beats). **Excluded from placement.** Its in-flight jobs are
  reclaimed by the reaper.
- `DRAINING` — administratively excluded from new placement; existing jobs finish.
  Used by `POST /admin/workers/{id}/drain` for graceful-drain scenarios.
- `DEAD` — heartbeat older than `eviction_timeout_s` (default 60 s); removed from
  the registry view but retained in history for the report.

`UNHEALTHY → HEALTHY` recovery is allowed and is a demo scenario.

---

## 6. Storage abstractions

Every AWS dependency sits behind an interface with a local implementation. This
is not aspirational — Phase 1 ships only local implementations, and the AWS ones
must not change a single line of scheduler or worker logic.

```python
class JobQueue(Protocol):        ...   # §3.4    Local  → SQS
class StateStore(Protocol):      ...   # §7      Local  → DynamoDB
class ArtifactStore(Protocol):   ...   # §9      Local  → S3
class WorkerRegistry(Protocol):  ...   #         Local  → DynamoDB-backed
class MetricsSink(Protocol):     ...   # §10     Local  → CloudWatch
```

Selected by `NIMBUS_BACKEND=local|aws` through a single factory in
`backend/services/factory.py`. There is exactly one place in the codebase that
branches on backend.

---

## 7. StateStore: access patterns first, keys second

Per the brief (§14), the access patterns are enumerated *before* the table design.

| # | Access pattern | Frequency |
|---|---|---|
| A1 | Get one job by `job_id` | high |
| A2 | Update job status / timestamps / assigned worker | very high |
| A3 | List jobs by status, newest first (dashboard) | high |
| A4 | List jobs for one benchmark run, all statuses | per-run, bulk |
| A5 | Append a scheduling decision | once per placement |
| A6 | Get the decision(s) for one job | on demand |
| A7 | List recent decisions across all jobs (dashboard) | high |
| A8 | Get current telemetry for all workers | very high (every placement) |
| A9 | Append telemetry sample for a worker | every 2–5 s per worker |
| A10 | Get execution-time history for (worker, job_type) | every placement |

**No pattern requires a table scan.** A3 and A7 are the ones that would tempt a
scan; both get a GSI.

### 7.1 Table design (Phase 5 — DynamoDB, single-table)

Table `nimbus-state`, `PK` (partition) + `SK` (sort).

| Entity | PK | SK | Notes |
|---|---|---|---|
| Job | `JOB#{job_id}` | `META` | A1, A2 |
| Job attempt | `JOB#{job_id}` | `ATTEMPT#{n:03d}` | attempt history |
| Decision | `JOB#{job_id}` | `DECISION#{n:03d}` | A6 |
| Worker telemetry (latest) | `WORKER#{worker_id}` | `CURRENT` | A8 |
| Worker telemetry (sample) | `WORKER#{worker_id}` | `TS#{iso8601}` | A9, TTL 24 h |
| Runtime stats | `STATS#{worker_id}` | `TYPE#{job_type}` | A10, EWMA + n |
| Benchmark run | `RUN#{run_id}` | `META` | |
| Benchmark job ref | `RUN#{run_id}` | `JOB#{job_id}` | A4 |

**GSI-1 `status-index`** — PK `GSI1PK = STATUS#{status}`, SK `GSI1SK = {created_at}#{job_id}` → A3, descending query with `Limit`.

**GSI-2 `decision-index`** — PK `GSI2PK = DECISIONS#{yyyy-mm-dd}`, SK `GSI2SK = {decided_at}#{job_id}` → A7, bounded by day partition.

Notes:
- Telemetry samples get a `ttl` attribute (24 h). Only `CURRENT` is read on the
  placement hot path — A8 is a `BatchGetItem` of 3 items, or served from an
  in-memory registry cache refreshed by the ingest path.
- Job status updates use a **conditional write** on the expected current status,
  which is how illegal concurrent transitions are rejected (§8).
- `A8` on the hot path is served from memory, not DynamoDB, in both backends. The
  store is the durable record; the registry is the read cache. Scheduling latency
  must not include a network round-trip per placement, or the latency metric
  measures DynamoDB rather than the scheduler.

### 7.2 Local implementation

`LocalStateStore` — dict-of-dicts behind a lock, with the *same* conditional-write
semantics (raises `ConditionFailed`) so that concurrency bugs surface locally.
Optional JSONL append for post-hoc analysis.

---

## 8. Idempotency and exactly-once-ish execution

SQS is at-least-once (`aws-native-scheduling.md` §3). Duplicate delivery is a
*when*, not an *if*. The rules:

1. **Execution token.** Each dispatch carries `execution_id = f"{job_id}#{attempt}"`.
2. **Worker-side dedupe.** The worker keeps an LRU of recently seen
   `execution_id`s. A repeat returns the cached result instead of re-executing.
3. **Store-side guard.** `RUNNING → COMPLETED` is a conditional write on
   `(status == RUNNING AND attempt == n)`. A late result from a superseded attempt
   fails the condition and is discarded with a logged warning.
4. **Artifact idempotence.** Outputs are written to a deterministic key
   `jobs/{job_id}/attempt-{n}/output.*`; re-execution overwrites in place.
5. **Metrics guard.** `JobsCompleted` is emitted from the state-transition handler,
   after the conditional write succeeds — never from the worker.

Result: a job may *execute* more than once (that is unavoidable with at-least-once
delivery plus crash recovery), but it **completes**, **counts**, and **writes**
exactly once. The brief's "avoid silently executing jobs multiple times" is met by
making duplicates detectable and non-mutating, and by logging every occurrence to
`nimbus.duplicate_execution` so the rate is a reported metric, not a hidden one.

---

## 9. ArtifactStore and S3 layout

Large payloads never enter the state store (brief §15).

```
s3://nimbus-artifacts-{suffix}/
  inputs/{job_id}/input.bin
  jobs/{job_id}/attempt-{n}/output.bin
  jobs/{job_id}/attempt-{n}/stdout.log
  benchmarks/{run_id}/manifest.json      # profile, seed, config, git sha
  benchmarks/{run_id}/raw.jsonl          # every job record, one per line
  benchmarks/{run_id}/decisions.jsonl    # every decision trace
  benchmarks/{run_id}/telemetry.jsonl    # every telemetry sample
  benchmarks/{run_id}/report.md
  benchmarks/{run_id}/figures/*.png
```

Inline payload cutoff: **256 KB**. Above that, the API stores the payload as an
artifact and the job carries `input_location` only.

`LocalArtifactStore` mirrors this layout under `./.nimbus/artifacts/`, so a
benchmark run directory is byte-identical in shape between local and AWS.

---

## 10. Observability: two distinct channels

The brief (§16) warns against conflating these. They are separate interfaces.

**Channel 1 — CloudWatch (`MetricsSink`), infrastructure/operational, 60 s.**
Namespace `Nimbus`. Dimensions: `Strategy`, `WorkloadClass`, `WorkerId` where
meaningful.

`JobsSubmitted` · `JobsCompleted` · `JobsFailed` · `JobsRetried` ·
`QueueDepthVisible` · `QueueDepthInFlight` · `ExecutionTimeMs` ·
`SchedulingLatencyMs` · `QueueWaitMs` · `WorkerHealthy` (0/1 per worker) ·
`DuplicateExecutions`

Emitted from the control plane via `PutMetricData`, batched (20 datums/call) to
keep cost and API volume bounded — see `docs/cost-model.md`.

*As built* (`backend/services/metrics.py`, emission in `backend/services/engine.py`):
the engine calls `metrics.emit(...)` and never CloudWatch. `build_metrics` picks
`NullMetricsSink` (`NIMBUS_METRICS_ENABLED=false`), `InMemoryMetricsSink` (local)
or `CloudWatchMetricsSink` (`aws`). Datums are **not** one per event: emissions
are folded per series (metric + dimension combination) into a `StatisticSet`
(count, sum, min, max), and each flush (`NIMBUS_METRICS_FLUSH_INTERVAL_S`,
default 60 s, plus a final one on `stop()`) sends one datum per series, 20 per
`PutMetricData` call. A failed call is counted and its chunk dropped; it never
raises into the scheduler. Emitting a metric with a dimension outside its allowed
set is a `ValueError`, because every dimension combination is a billable series.

| Metric | Dimensions | Emitted |
|---|---|---|
| `JobsSubmitted` | `WorkloadClass` | `submit()`, after enqueue |
| `JobsCompleted` | `Strategy`, `WorkloadClass` | after the conditional completion write succeeds (guard 5) |
| `ExecutionTimeMs` | `Strategy`, `WorkloadClass` | same point; the worker-reported execution time |
| `JobsFailed` | `Strategy`, `WorkloadClass` | the dead-letter and retries-exhausted branches |
| `JobsRetried` | `Strategy`, `WorkloadClass` | the requeue branch |
| `SchedulingLatencyMs` | `Strategy` | after every decision is recorded (`decision_latency_us / 1000`) |
| `QueueWaitMs` | `Strategy`, `WorkloadClass` | after `ACCEPTED`, first attempt only (`scheduled_at - created_at`) |
| `DuplicateExecutions` | `Strategy` | both stale-result sites |
| `QueueDepthVisible`, `QueueDepthInFlight` | none | gauge sample, from `queue.depth()` |
| `WorkerHealthy` | `WorkerId` | gauge sample; 1 if `HEALTHY`, else 0 |

Gauges are sampled in `tick()` every `NIMBUS_METRICS_SAMPLE_INTERVAL_S` (default
10 s), not on every tick. `stats()` and `reliability()` are unchanged. Tested
against a hand-written `FakeCloudWatch`; **nothing has been written to real
CloudWatch.**

**Channel 2 — Nimbus scheduling telemetry (`WorkerRegistry`), 2–5 s.**
CPU%, memory%, active jobs, queue depth, load trend, heartbeat age. Lives in
process memory + `StateStore`, **not** in CloudWatch. It is a scheduler input, at
a frequency CloudWatch is neither designed nor priced for (3 workers × 5 metrics
at 5 s would be 60× the volume of Channel 1 for no operational benefit).

The dashboard reads Channel 2 through the Nimbus API. Nothing in the scheduler
ever reads CloudWatch.

---

## 11. Configuration

One `NimbusConfig` (pydantic-settings), env-prefixed `NIMBUS_`, single source of
truth, serialised into every benchmark manifest so runs are reproducible.

| Key | Default | Meaning |
|---|---|---|
| `NIMBUS_BACKEND` | `local` | `local` \| `aws` |
| `NIMBUS_STRATEGY` | `fifo` | active scheduler |
| `NIMBUS_WORKER_COUNT` | `3` | local fleet size |
| `NIMBUS_WORKER_SLOTS` | `2` | max concurrent jobs per worker |
| `NIMBUS_WORKER_CPU_UNITS` | `2048` | declared CPU capacity (1 vCPU = 1024); must be ≥ `slots × max job cpu_units` or slots are unreachable |
| `NIMBUS_WORKER_MEMORY_MB` | `4096` | declared memory capacity |
| `NIMBUS_TELEMETRY_ENABLED` | `true` | collect OBSERVED state; never affects placement |
| `NIMBUS_TELEMETRY_INTERVAL_S` (or `..._SECONDS`) | `2.0` | psutil sample period |
| `NIMBUS_TELEMETRY_HISTORY` | `300` | samples retained per worker |
| `NIMBUS_TELEMETRY_WINDOW` | `3` | smoothing window; **changing it changes adaptive-v1's identity** |
| `NIMBUS_TELEMETRY_MAX_AGE_SECONDS` | `6.0` | telemetry older than this is stale |
| `NIMBUS_HEARTBEAT_INTERVAL_S` | `5.0` | heartbeat period |
| `NIMBUS_HEARTBEAT_TIMEOUT_S` | `15.0` | → `UNHEALTHY` |
| `NIMBUS_EVICTION_TIMEOUT_S` | `60.0` | → `DEAD` |
| `NIMBUS_JOB_TIMEOUT_S` | `300.0` | default per-job wall clock |
| `NIMBUS_MAX_ATTEMPTS` | `3` | then DLQ + `FAILED` |
| `NIMBUS_VISIBILITY_TIMEOUT_S` | `60.0` | queue lease |
| `NIMBUS_SCHEDULER_TICK_S` | `0.05` | scheduler loop period |
| `NIMBUS_SEED` | `1337` | benchmark determinism |
| `NIMBUS_WORKER_TRANSPORT` | `inprocess` | `inprocess` \| `http`; `http` needs `NIMBUS_WORKER_ENDPOINTS` (checked by `factory.validate`) |
| `NIMBUS_WORKER_ENDPOINTS` | *(empty)* | `id=http://host:8080,...`; ids must be unique, scheme http(s) |
| `NIMBUS_DISPATCH_TIMEOUT_S` | `5.0` | `POST /execute` read timeout |
| `NIMBUS_WORKER_PROBE_TIMEOUT_S` | `2.0` | `GET /health`, `/telemetry`, `/stats` |
| `NIMBUS_METRICS_ENABLED` | `true` | `false` -> null sink |
| `NIMBUS_METRICS_FLUSH_INTERVAL_S` | `60.0` | `PutMetricData` cadence |
| `NIMBUS_METRICS_SAMPLE_INTERVAL_S` | `10.0` | queue-depth / worker-health gauge cadence |

---

## 12. AWS resource inventory (Phase 5)

| Resource | Purpose | Terraform file |
|---|---|---|
| VPC, 1 public + 1 private subnet, NAT or VPC endpoints | network isolation | `networking.tf` |
| 3 × EC2 `t3.small` (workers) | data plane | `ec2.tf` |
| 1 × EC2 `t3.small` (control plane: API + scheduler) | control plane | `ec2.tf` |
| SQS `nimbus-jobs` + `nimbus-jobs-dlq` | pending queue, redrive `maxReceiveCount=3` | `sqs.tf` |
| DynamoDB `nimbus-state` on-demand + 2 GSIs | control-plane state | `dynamodb.tf` |
| S3 `nimbus-artifacts-*`, SSE-S3, public access blocked, versioning off | artifacts | `s3.tf` |
| CloudWatch log group `/nimbus/*`, 7-day retention; custom metrics | observability | `cloudwatch.tf` |
| IAM instance profiles (worker ≠ control plane) | least privilege | `iam.tf` |
| Security groups | scheduler → worker `:8080`; worker → control plane `:8000` (results); no public worker ingress | `networking.tf` |
| *(conditional)* AWS Batch CE + queue + job definition | native baseline | `batch.tf` |

The control plane has a fixed private IP (`cidrhost(<public subnet>, 10)`) so
workers can be handed `NIMBUS_CONTROL_PLANE_URL` without depending on the
control-plane instance, which in turn lists the workers' addresses in
`NIMBUS_WORKER_ENDPOINTS`; that would otherwise be a Terraform cycle. `terraform
fmt -check` and `terraform validate` pass; `apply` has never been run.

`batch.tf` is created **only if** the Batch comparison is actually run
(`docs/experiments.md` §6). Otherwise it does not exist.

**IAM split:**
- Worker role: `s3:GetObject`/`PutObject` on `arn:…:nimbus-artifacts-*/*` only;
  `logs:PutLogEvents` on its own log group. **No SQS access, no DynamoDB access** —
  a worker that cannot touch the queue cannot bypass the scheduler, which makes
  the control/data-plane split an IAM-enforced invariant rather than a convention.
- Control-plane role: SQS on the two queues; DynamoDB on the table + GSIs;
  `cloudwatch:PutMetricData` scoped to namespace `Nimbus`; S3 read for artifacts.

Cost: see [`cost-model.md`](./cost-model.md).

---

## 13. Repository layout

```
nimbus/
├── backend/
│   ├── api/            jobs, workers, scheduler, benchmarks, admin, internal
│   ├── models/         job, worker, telemetry, decision, enums, state machine
│   ├── services/       engine, registry, reaper, retry, benchmarks, factory,
│   │                   remote (HTTP dispatcher), metrics (MetricsSink + CloudWatch)
│   │   ├── queue/      base.py, local.py, sqs.py
│   │   ├── store/      base.py, local.py, dynamodb.py
│   │   └── artifacts/  base.py, local.py, s3.py
│   └── aws/            clients.py — the only module that imports boto3, lazily
├── scheduler/          base.py (policy ABCs) views.py (telemetry boundary)
│                       ordering.py strategy.py replay.py
│                       placement/ round_robin.py static_resource_aware.py adaptive.py
├── worker/             agent.py api.py batch_entrypoint.py executor.py telemetry.py heartbeat.py workloads/
├── benchmark/          profiles.py generator.py pressure.py runner.py metrics.py
│                       manifest.py analysis.py report.py cost.py
├── frontend/           React + TS + Vite + Tailwind + Recharts (read-only dashboard)
├── infra/              Terraform — complete, never applied
├── tests/              unit/ integration/ failure/
├── scripts/            demo_phase*.py generate_report.py demo/{deploy,teardown}.sh
└── docs/
```

---

## 14. Technical risks

| # | Risk | Impact | Mitigation |
|---|---|---|---|
| R1 | **Telemetry lag → herding.** CPU% describes the last sample window; several jobs dispatched inside one interval all see the same stale "idle" worker and pile onto it. | Adaptive loses to Resource-Aware. Would silently invalidate the hypothesis test. | Reserve declared resources at dispatch and add them to the observed reading (§ `scheduling.md` §5.4). Measure it: report placement concentration within one telemetry interval as a diagnostic. |
| R2 | **Local workers share one host.** Three processes on one 20-core machine contend for a shared CPU and page cache, so "worker CPU%" is not independent. | Local numbers may not transfer to EC2. | Pin workers to disjoint core sets (`psutil.Process.cpu_affinity`); report per-worker CPU as a fraction of *its own* affinity set; treat local results as directional only and re-run the headline experiment on EC2. |
| R3 | **Benchmark confounding by machine noise.** | Bogus deltas. | Fixed seeds, ≥5 trials, randomised strategy order, report median + IQR, paired comparison on identical job sequences, report CI not just means. |
| R4 | **Unfair Batch comparison** (`RUNNABLE` wait + image pull dominate short jobs). | Dishonest headline. | Pre-warm CE to `minvCpus>0`, pre-pull image, measure from `RUNNING`, *and* report end-to-end separately. If not affordable, publish an architectural comparison and no number. |
| R5 | **Scheduler is a bottleneck.** Decision latency could dominate at high throughput. | Confounds throughput results. | Measure `SchedulingLatencyMs` per decision as a first-class metric and publish it; keep the hot path in memory (§7.1). |
| R6 | **AWS spend overrun.** | Budget. | Local-first; `terraform destroy` in the demo script; budget alarm; `t3.small`; no NAT gateway if VPC endpoints suffice. |
| R7 | **Duplicate execution from at-least-once delivery.** | Corrupt metrics. | §8, plus `DuplicateExecutions` as a reported metric. |
| R9 | **Static baseline degenerates at low load.** With an idle fleet every worker scores identically and the `worker_id` tie-break sends all jobs to `worker-1`. Measured: worse balance than round-robin below ~20% fleet utilisation. | Phase 3 would beat Static for the wrong reason. | Report offered load with every result; run comparisons only in a loaded regime. `scheduling.md` §5.4. |
| R11 | **adaptive-v1 gets retuned after seeing results.** | Any comparison against archived runs becomes meaningless. | Formula, weights, smoothing window and freshness threshold are pre-registered in `docs/adaptive-v1-preregistration.md` and pinned by tests. Every trace and manifest records `algorithm_version`; a change is `adaptive-v2` and a new experiment. |
| R12 | **Adaptive oscillates.** Job lands → CPU rises → scheduler flees → CPU falls → scheduler floods. | Throughput loss that looks like a scheduling failure. | Shipped with no damping *on purpose*; `switch_rate` and `concentration_max` measure it per run so it is documented before anything is added to suppress it. |
| R10 | **Telemetry boundary erodes.** A future policy reads `state.observed` directly and the static control silently becomes telemetry-aware. | Silent loss of the experimental control — nothing fails. | Structural: static policies receive `StaticWorkerView`, which has no telemetry fields (`slots=True`, so none can be attached). Field set pinned by test. Replay verifies decisions reproduce from static state alone. |
| R8 | **Weight vectors tuned on the test workload** → overfitting to the benchmark. | Invalidates the result. | Fix weights from first principles *before* running experiments, record them in git, and report any change as a separate labelled configuration. |

---

## 15. Implementation plan

| Phase | Deliverable | Exit criterion |
|---|---|---|
| **1** | Local API, `LocalJobQueue`, FIFO scheduler, 3 local workers, real workloads, status tracking | `POST /jobs` → `COMPLETED` with a real result; integration test green |
| 2 | Priority, Resource-Aware, Adaptive + `scoring.py`; deterministic scheduler unit tests | Same fixture fleet ⇒ each strategy makes its documented choice |
| **3** | adaptive-v1: telemetry-weighted placement, freshness handling, adaptive replay, algorithm versioning | **done** — overloading W1 demonstrably changes Adaptive's pick; end-to-end effect measured, see `experiments.md` |
| **4** | Reaper, timeouts, retries, idempotency, DLQ | **done** — `tests/failure/` kills a worker and the job completes elsewhere |
| **5** | Terraform: EC2, SQS, DynamoDB, S3, CloudWatch, IAM | **code complete, never applied** — adapters and `infra/` are written and tested against fakes; the criterion "same suite green against `NIMBUS_BACKEND=aws`" is **unmet**, because no AWS resources were provisioned |
| **6** | Dashboard | **done** — live fleet, queue depth, decision trace (`frontend/`) |
| **7** | Benchmark framework | **done** — `POST /benchmarks` → archived `raw.jsonl` + report |
| **8** | AWS Batch baseline *or* documented refusal | **done, as a refusal** — [`aws-batch-baseline.md`](./aws-batch-baseline.md) |
| **9** | Analysis | **done** — [`results.md`](./results.md), regenerated by one command |

**Phases 1–9 are implemented, with one exit criterion knowingly unmet.** Phase 5's
criterion is *the same test suite green against `NIMBUS_BACKEND=aws`*, and that
has not been demonstrated: `terraform apply` was never run, so the AWS code path
has never executed against the real services. The adapters are covered by unit
tests against hand-written fakes of the SQS, DynamoDB and S3 surfaces — which
pins our understanding of those semantics, and is not the same thing as
validating it. Recorded here rather than in a footnote, because a phase table
that showed Phase 5 simply "done" would be claiming something untrue.

`adaptive-v1` remains the only policy that consumes observed telemetry; the
static policies are still structurally unable to, and `unit/test_views.py` pins
that boundary from both sides.
