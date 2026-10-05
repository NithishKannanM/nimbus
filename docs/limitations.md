# Nimbus Limitations

Written at design time, before results exist, so that it cannot be quietly
trimmed to fit whatever the numbers turn out to say. Updated as measurements
land.

---

## 1. Scope limitations (deliberate)

Nimbus does not implement, and does not claim to implement:

| Not implemented | AWS equivalent that does |
|---|---|
| Preemption of running jobs | — |
| Job migration after start | — |
| Gang / multi-node scheduling | AWS Batch multi-node parallel jobs |
| DAG dependencies | AWS Batch `dependsOn` |
| Cross-tenant fairness | Batch fair-share policies; SQS fair queues |
| Autoscaling the fleet | EC2 Auto Scaling (incl. predictive scaling) |
| Instance-type / Spot selection | Batch allocation strategies |
| Multi-AZ or multi-region durability | every managed AWS service |
| Backfill / lookahead | — |
| ML-based prediction | — (out of scope by design, brief §6) |
| **Priority ordering** | Batch job priority (designed, not implemented) |

The fixed three-worker fleet is an **experimental control**, not a limitation of
ambition: a fleet that resizes mid-run makes every latency measurement
uninterpretable.

---

## 2. Architectural limitations

**The scheduler is a single point of failure and a throughput ceiling.**
A centralised decision point is what makes strategies comparable — it is the
right choice for the experiment and the wrong choice for production. Scheduling
latency is measured and published (`experiments.md` §5.1) precisely so a reader
can see where the ceiling is. There is no leader election in Phase 1–4; a
DynamoDB-conditional-write lease is sketched in `architecture.md` §3.3 and not
built.

**Push dispatch puts scheduler→worker connectivity on the critical path.**
A network partition that leaves a worker alive but unreachable looks exactly like
a dead worker, and its jobs are re-placed while it may still be running them.
This is why idempotency (`architecture.md` §8) is mandatory rather than
defensive, and why duplicate executions are a *reported metric*.

**Execution is at-least-once, not exactly-once.** With at-least-once queue
delivery plus crash recovery, a job can *execute* twice. Nimbus guarantees it
**completes, counts and writes** once. Workloads must be pure functions of their
payload; a workload with external side effects would break this and none is
included.

**One job, one worker, one slot's worth of declared resources.** No fractional or
elastic resource allocation.

**A locally timed-out job is abandoned, not killed** *(Phase 4)*. When the reaper
reclaims a job for exceeding its wall-clock timeout, the control plane frees the
slot, releases the reservation and re-places the job — but the underlying
process keeps running to completion.

`ProcessPoolExecutor` offers no way to terminate one in-flight task: killing its
child breaks the pool for every other task on that worker, so the choice is
between abandoning one job and failing all of them. The consequence is real and
measurable: for the remainder of that execution Nimbus **under-counts the load
on that host**, because the work is still consuming CPU while appearing in no
reservation. `abandoned_executions` reports the rate, and the abandoned result
is discarded by the execution-id guard rather than credited to the attempt that
replaced it.

The HTTP worker (`worker/api.py`) wraps the same `LocalWorkerAgent` and its
process pool, so `POST /abandon` on an EC2 worker has the same effect: the
execution is marked abandoned and its result discarded, and the process runs on.
A process-per-job worker could signal it, but that is not what is built. It is
recorded rather than fixed because fixing it locally would mean replacing the
pool with a process-per-job model, which would change execution timing and make
every archived benchmark run incomparable to future ones.

**Remote workers cannot be hard-failed from the control plane** *(HTTP data
plane)*. With `NIMBUS_WORKER_TRANSPORT=http`, `fail_worker(hard=True)` raises
`NotImplementedError`: crashing a pool on another host is that host's business.
`hard=False` (a partition) works. The failure-injection tests exercise real
process kills only against the local transport, so the reaper's behaviour on a
genuinely dead remote host rests on the `/health` probe path, which has been
tested against mocks and never against a failing EC2 instance.

**Abandon over HTTP is fire-and-forget, and a read timeout is ambiguous.**
`abandon` is queued, not confirmed, and a timed-out `POST /execute` may have
been accepted. Both are absorbed by execution-id dedupe rather than prevented,
so a dispatch timeout can cost a duplicate execution, which is counted.

**The control plane needs a fixed private IP, and workers are baked into its
configuration.** To avoid a Terraform cycle, `infra/ec2.tf` pins the control
plane to `cidrhost(<public subnet>, 10)`, and its `user_data` embeds every
worker's address in `NIMBUS_WORKER_ENDPOINTS`. Replacing a worker changes that
`user_data`, which may force the control plane to be recreated. There is no
service discovery and no elastic fleet.

**An orphaned job waits out the visibility timeout.** A job in a live state whose
lease the control plane has lost cannot be made visible again: SQS offers no way
to delete or reset a message without its receipt, and `LocalJobQueue` reproduces
that. The visibility timeout is therefore the recovery bound for an orphan — a
reason to keep it short, not a reason to work around it.

---

## 3. Measurement limitations

**Local workers share a host.** Three worker processes on one machine contend for
one CPU package, one memory bus and one page cache, so per-worker CPU% is not
independent — loading worker 1 perturbs workers 2 and 3. Mitigated by
`cpu_affinity` pinning to disjoint core sets, but cache and memory-bandwidth
contention remain. **Local results are directional; the headline experiment must
be re-run on separate EC2 instances**, and any local-only result is labelled as
such.

**A single job must not be allowed to use more than one core.** numpy's matmul
is multi-threaded by default, so an unrestricted `matrix_multiply` job spreads
across every core in its worker's affinity set. Measured locally: size-1800
matmul takes 94 ms unrestricted and 338 ms pinned to one thread. Left
unrestricted, one job saturates a worker the scheduler believes is half idle,
and both "slots" and declared `cpu_units` become fiction. Worker pool children
therefore set `OMP_NUM_THREADS` and the equivalent OpenBLAS/MKL/NumExpr/vecLib
variables to 1. The consequence is that Nimbus measures *scheduling* efficiency,
not *intra-job* parallelism, and the workloads are deliberately single-threaded;
a fleet running genuinely multi-threaded jobs is outside what these results say
anything about.

**adaptive-v1 has no stabilisation, on purpose.** No hysteresis, no damping, no
anti-flap logic beyond the pre-registered 3-sample smoothing window. Reacting to
a lagging signal can oscillate — job lands, CPU rises, scheduler flees, CPU
falls, scheduler floods. That is measured (`switch_rate`,
`concentration_max`) rather than pre-emptively engineered away, because adding
stabilisation before observing the problem would make it impossible to say
whether it was ever needed. If oscillation is observed it is a documented
finding, and any fix is `adaptive-v2`.

**adaptive-v1's memory signal cannot discriminate between local workers.** Three
local workers share one machine's RAM, so `memory_percent` is identical for all
of them and an external memory stressor raises it equally everywhere. Only
`process_memory_mb / declared_memory_mb` is worker-scoped, and that largely
duplicates what RESERVED already captures. **Profile C therefore cannot
demonstrate memory discrimination locally**, and a null result there is expected
for a measurement reason rather than an algorithmic one. This was stated in the
pre-registration before the experiment ran, not after seeing the result.
Resolved only by Phase 5, where each worker owns its host.

**Telemetry is a lagging signal.** Every reading adaptive-v1 scores on describes
the recent past — up to `telemetry_interval_s` old at best, and smoothed over a
~6 s window by design. It is a *sample*, not instantaneous truth. adaptive-v1
handles absent and stale readings explicitly (§7.4 of `scheduling.md`) but
cannot handle a worker whose load changed since the last sample; the
`concentration_max` metric exists to detect the herding that causes.

**R9 is active in the loaded regime, not just at low load.** Phase 2 identified
the `worker_id` tie-break degeneracy as a *low-load* problem and the Phase 3
pre-registration gated on fleet utilisation to avoid it. Measurement showed the
static policies decide **35–36% of placements by the tie-break even at 0.25–0.85
fleet utilisation**. So the load gate did not do what it was designed to do, and
part of adaptive-v1's measured advantage over `static_resource_aware` is
"eliminating arbitrary tie-breaks" rather than "using telemetry content". The
two are hard to separate — a continuous signal is *why* the ties disappear — but
they are not the same claim. `fifo` never ties, which is why adaptive-vs-`fifo`
is treated as the clean comparison.

**The pre-registered materiality criterion was a bad choice.** It required
non-overlapping per-run ranges, which on a bimodal distribution is close to
unsatisfiable at N≈9 regardless of effect size: one unlucky run in either arm
destroys it. It was written before the bimodality was known. The criterion is
reported as failing rather than quietly edited, and `adaptive-v2`'s
pre-registration should use a rank-based test with a stated effect-size floor.

**Profile B makespan is dominated by a single placement.** Whichever large I/O
job lands on the saturated worker runs 10–40× slower there and sets the makespan
essentially on its own. That makes makespan a high-variance statistic in this
profile; it is why N was raised from the pre-registered 5 to 8–9, and why the
share of work sent to the pressured worker is reported alongside it as the
lower-variance measure of what the scheduler actually did.

**The pre-registered load-regime gate is a proxy, and it mis-fires on Profile B.**
The gate (mean fleet utilisation ≥ 0.50) was chosen to guard against R9, where
Static degenerates because scores tie. But under external pressure, mean fleet
utilisation *falls* — the pressured worker slowly drains a backlog while the
others idle — so Profile B can fail the gate as a *consequence of the effect
being studied*. The gate is still reported for every run, and `tie_rate` (added
before Phase 3 experiments ran, and disclosed as such) measures the R9 confound
directly rather than through a proxy. Runs are assessed on both.

**The static baseline degenerates at low load, and this could fake a Phase 3
result.** With an idle fleet every worker has zero reservations, scores tie, and
the deterministic `worker_id` tie-break sends every job to `worker-1`. Measured
below ~20% fleet utilisation: `static_resource_aware` had *worse* utilisation
balance than plain round-robin FIFO across every profile. The behaviour is
correct per specification, but it means a Phase 3 comparison run at low load
would show Adaptive beating Static because Static degenerates under ties, not
because telemetry carries information. Every reported run must state its offered
load, and Phase 3 comparisons must run where reservations meaningfully differ
between decisions. See `docs/scheduling.md` §5.4.

**Host-scoped memory telemetry cannot discriminate between local workers.**
Three local workers share the machine's RAM, so `memory_percent`,
`available_memory_mb` and `load_average_1m` read identically for all of them.
Only `process_memory_mb` (RSS of a worker's own executor children) and
`cpu_percent` (mean over its pinned core set) are worker-scoped locally. Both
scopes are recorded and labelled, never conflated. On EC2 (Phase 5) each worker
owns its host and the host-scoped fields become genuinely per-worker — which
means **Profile C (memory pressure) is materially weaker locally than Profile B
(CPU pressure)** and its results should be treated as provisional until re-run
on separate instances.

**Development machine contamination.** Experiments ran on a shared workstation.
Unpressured workers read 6–30% CPU at idle from non-Nimbus processes. This
affects absolute telemetry values, not within-run comparisons (all arms share
the condition), but it is a reason the headline experiments must be repeated on
dedicated instances.

**Phase 2 baseline figures are single-trial.** They are descriptive. Any claim
in the final report requires the N≥5 protocol in `docs/experiments.md` §2.2.
The one repeated measurement so far is the telemetry ON/OFF control (3 trials
per arm), and even there the throughput delta (−1.7%) is smaller than the
within-arm spread, so it supports no conclusion beyond "the duty cycle is
0.134%".

**`psutil` CPU% is a sampled average over an interval**, not an instantaneous
reading. Every telemetry value the scheduler sees is up to
`telemetry_interval_s` stale. This is not an implementation defect — it is the
central property being tested, and it is why the anti-herding correction
(`scheduling.md` §5.4) exists and why staleness is recorded per decision.

**p99 from a 100-job run is one data point.** Tail statistics are pooled across
trials and always accompanied by N.

**The weights were chosen by the same people evaluating them.** Mitigated by
fixing them from stated first principles before any run and committing them to
git; any subsequent change is reported as a separate labelled configuration, not
folded into the original result. It is still a real threat to validity and it is
named in the report.

**Benchmark profiles were also chosen by us.** Mitigated by deliberately
including profiles predicted to *reject* the hypothesis (`long_tail`, saturated
`balanced`) and by reporting all of them, including the ones where Nimbus loses.

**`t3` burstable CPU credits** can silently change a worker's performance
mid-experiment. Monitored via `CPUCreditBalance`; credit-exhausted trials are
discarded and re-run, or the fleet moves to `c6i.large`.

---

## 4. Comparison limitations

**The AWS Batch comparison did not happen, and no Batch number is published.**
*(Phase 8 — resolved.)* Batch's `RUNNABLE` wait and image-pull-dominated
`STARTING` phase (`aws-native-scheduling.md` §1.4) dwarf the placement effect for
short jobs, so `experiments.md` §6.2 listed seven conditions that must all hold
before any Batch number could be published. Four are unmet and two are designed
but unexercised, because **no AWS resources were ever provisioned**: the whole
project ran locally and `terraform apply` was never executed.

The rule was *all seven, or no number*, so: no number. The full disclosure is
[`aws-batch-baseline.md`](./aws-batch-baseline.md), written in the form §6.3
requires. This was a pre-committed decision and is recorded as a result rather
than as an omission.

**Nothing in this project was ever run on AWS.** *(Phase 5 — code written, never
applied.)* `infra/` is complete Terraform and `backend/services/factory.py`
selects real SQS, DynamoDB and S3 adapters, all covered by tests against
hand-written fakes of those APIs. None of it has been validated against the
services themselves. Every measurement in `results.md` is local, so the headline
experiment still needs re-running on real hardware before any of it transfers —
which is the same caveat `§3` makes about shared-host telemetry, now with the
additional one that the AWS code path has never executed.

**Nimbus is compared against its own baselines, which we also wrote.** The FIFO
and Priority baselines use round-robin placement rather than "first free"
specifically to avoid a straw man (`scheduling.md` §4). Static Resource-Aware is
modelled on ECS `binpack` and is the *serious* comparator; H1c
(`experiments.md` §1) treats beating it, not beating FIFO, as the real test.

**Any result is specific to this fleet, these workloads and this scale.** Three
homogeneous 2-vCPU workers running 100–500 jobs. Nothing here generalises to
heterogeneous fleets, thousands of workers, or hour-long jobs, and the report
must not imply otherwise.

---

## 5. Security limitations

- Single-tenant. No authentication on the Nimbus API in Phases 1–7; it is bound
  to localhost locally and to a private security group on AWS. **This is not a
  production posture** and is stated in the README.
- Administrative endpoints (`/admin/*`) are mounted on a separate router,
  **not registered at all** unless `NIMBUS_ENABLE_ADMIN=1`, and must never be
  exposed publicly. Since there is no authentication anywhere before Phase 7,
  "the route does not exist" is the entire access-control story for the routes
  that can crash a worker or drain the fleet. A flag that has to be deliberately
  set is a weaker guarantee than authentication, and stating that is more useful
  than implying otherwise.
- The Phase 6 dashboard is **read-only** and is served by the control plane from
  `frontend/dist`. It can submit no jobs, change no strategy and touch no worker;
  every destructive control lives behind the admin gate above.
- Workloads execute arbitrary *parameters*, never arbitrary *code*. Job types are
  a closed enum resolved through a registry; there is no `eval`, no dynamic
  import from payload, and payload fields are validated by pydantic models with
  explicit bounds.
- The worker API (`worker/api.py`: `/execute`, `/abandon`, `/health`, ...) and the
  control plane's `/internal/results` have **no authentication** beyond the
  security group in front of them. Anything that can reach `:8080` can run a
  (validated, closed-enum) workload on a worker or abandon an execution; anything
  that can reach `:8000` can post a result.
- The HTTP data plane, the Batch entry point and the CloudWatch metrics path are
  written and tested locally only. None has run on real AWS, so their behaviour
  on a real network, under real IAM and against real CloudWatch is unverified.
- No encryption in transit between scheduler and worker in Phase 5 (plain HTTP
  inside a private security group). Adding TLS is straightforward and untaken.
- IAM least privilege *is* enforced: the worker role has no SQS and no DynamoDB
  access, which makes the control/data-plane split an infrastructure invariant
  rather than a convention (`architecture.md` §12).

---

## 6. Known open questions

Recorded now; answered by measurement, or explicitly left open in the report.

0. **Answered in Phase 2 (partially).** Does observed telemetry differ from the
   declared/reserved model in ways the static scheduler misses? *Yes,
   measurably*: under external CPU pressure, `worker-1` was observed at 100%
   while Nimbus's model reported its full declared capacity free, and
   `static_resource_aware` was **73% slower than round-robin FIFO** as a result.
   That establishes a gap exists. It is *not* evidence that consuming telemetry
   would close it — Phase 3 still has to show that, and must beat **both**
   baselines to mean anything.
1. Does the anti-herding correction (`scheduling.md` §7.4) actually help, or does
   it just turn Adaptive into Resource-Aware with extra steps? → ablation
   `NIMBUS_ADAPTIVE_ANTI_HERD=0`.
2. Is the runtime-history term (δ) informative on a homogeneous fleet, where by
   construction every worker should be equally fast? → runtime-estimate error and
   term-contribution breakdown (`experiments.md` §5.5). It is plausible that δ
   contributes nothing until the fleet is heterogeneous, and that would be worth
   reporting.
3. What is the load regime in which telemetry-aware placement pays off — and is
   that regime wide enough to matter? → λ sweep on `balanced`.
4. Does class-conditioned weighting beat a single fixed weight vector? → ablation
   with uniform weights across all classes.
5. Where does the scheduler become the bottleneck? → decision latency vs. offered
   load.

Question 4 is the one most likely to embarrass the design, since it tests whether
the workload taxonomy — the conceptual centre of the project — earns its
complexity. It is in the plan for exactly that reason.
