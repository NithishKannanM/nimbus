# AWS-Native Scheduling: What Already Exists

**Status:** research baseline, written before implementation.
**Sources:** current AWS official documentation (fetched 2026-08-21). Links inline.
**Purpose:** establish, on the record, that AWS already provides production job
scheduling, so that Nimbus can be positioned honestly as an *experiment* rather
than a replacement.

> Nimbus does **not** claim that AWS lacks workload scheduling. AWS Batch, Amazon
> ECS task placement, Amazon SQS and EC2 Auto Scaling are mature, production
> scheduling and placement systems. This document describes what they do, so that
> the narrow thing Nimbus investigates can be stated precisely.

---

## 1. What AWS Batch already solves

[AWS Batch job scheduling](https://docs.aws.amazon.com/batch/latest/userguide/job_scheduling.html)
is a managed batch computing service. It already provides:

### 1.1 Queueing and ordering

- **Job queues with a default FIFO strategy.** "If you don't specify a scheduling
  policy when you create a job queue, the AWS Batch job scheduler defaults to a
  first-in, first-out (FIFO) strategy."
- **Job priority** on the queue, and **queue priority** when several queues map to
  the same compute environment.
- **Explicit ordering via dependencies** — the `dependsOn` parameter of
  [`SubmitJob`](https://docs.aws.amazon.com/batch/latest/APIReference/API_SubmitJob.html)
  expresses DAG-style precedence. This is strictly more expressive than anything
  Nimbus implements.

### 1.2 Fair-share scheduling

[Fair-share scheduling policies](https://docs.aws.amazon.com/batch/latest/userguide/fair-share-scheduling.html)
attach to a job queue and replace FIFO ordering. The documented controls:

| Control | Meaning (per AWS docs) |
|---|---|
| `shareDecaySeconds` | Window used to compute each share identifier's fair-share percentage. Zero means "only current usage is measured"; longer decay gives more weight to history. |
| `computeReservation` | Prevents one share identifier from consuming the whole queue. Reserved ratio is `(computeReservation/100)^ActiveFairShares`. |
| `weightFactor` | Default `1`. Lower value gets *more* compute: "jobs that use a share identifier with a weight factor of 0.125 (1/8) are assigned eight times the compute resources of jobs that use a share identifier with a weight factor of 1." |

A share identifier is *active* while it has jobs in `SUBMITTED`, `PENDING`,
`RUNNABLE`, `STARTING` or `RUNNING`.

**This is genuinely history-aware scheduling.** Batch already looks at *past
consumption* over a decay window. Any Nimbus claim of "AWS scheduling is static"
would be false. What Batch's fair-share input is, precisely, is *consumed share of
queue capacity per tenant* — not *observed instantaneous resource pressure on a
specific host*.

### 1.3 Capacity/instance-type selection

[Allocation strategies](https://docs.aws.amazon.com/batch/latest/userguide/allocation-strategies.html)
choose which EC2 instance types to launch when more capacity is needed:

- `BEST_FIT` (default) — best fit for job requirements, preferring lowest cost;
  will wait for capacity rather than diversify.
- `BEST_FIT_PROGRESSIVE` — adds instance types large enough for queued jobs,
  preferring lower cost per vCPU.
- `BEST_FIT_PROGRESSIVE_ORDERED` — honours the caller's `instanceTypes` order.
- `SPOT_CAPACITY_OPTIMIZED`, `SPOT_PRICE_CAPACITY_OPTIMIZED`,
  `SPOT_CAPACITY_OPTIMIZED_PRIORITIZED` — Spot-interruption-aware selection.

This is *vertical* placement (what machine shape to buy) and it is informed by
real fleet signals AWS holds and Nimbus cannot see (Spot pool interruption risk,
regional capacity).

### 1.4 Job lifecycle and retries

[Job states](https://docs.aws.amazon.com/batch/latest/userguide/job_states.html):
`SUBMITTED → PENDING → RUNNABLE → STARTING → RUNNING → SUCCEEDED | FAILED`.
Automated retries move a failed attempt back to `RUNNABLE`. Logs land in
CloudWatch Logs under `/aws/batch/job`.

Two operationally important details that matter for any latency comparison:

- Jobs "can remain in `RUNNABLE` indefinitely when sufficient resources are
  unavailable."
- `STARTING` absorbs container image pull time: "if it takes three minutes to pull
  the image for your job, your job will be in the STARTING state for three
  minutes. … STARTING is not included in timeouts; duration starts at RUNNING."

**Implication for Nimbus benchmarking:** a naive end-to-end wall-clock comparison
between Batch and Nimbus mostly measures *container cold start and capacity
provisioning*, not *placement quality*. Section 6 of `docs/experiments.md` handles
this; if we cannot control it, we report an architectural comparison instead of a
number.

---

## 2. What ECS task placement already solves

[How Amazon ECS places tasks](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement.html)
documents a four-step selection process for the EC2 launch type:

1. Identify container instances that satisfy the **CPU, GPU, memory and port
   requirements in the task definition**.
2. Identify instances that satisfy the **task placement constraints**.
3. Identify instances that satisfy the **task placement strategies**.
4. Select the container instances for task placement.

Constraints are **binding**; strategies are **best effort** ("Amazon ECS still
attempts to place tasks even when the most optimal placement option is
unavailable").

### 2.1 Placement strategies

From [task placement strategies](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-placement-strategies.html):

| Type | Fields | Behaviour |
|---|---|---|
| `binpack` | `cpu`, `memory` | "Tasks are placed on container instances so as to leave the least amount of unused CPU or memory. This strategy minimizes the number of container instances in use." |
| `random` | — | Random placement. |
| `spread` | `instanceId`/`host`, or any attribute e.g. `attribute:ecs.availability-zone` | Even distribution across the specified value. |

Strategies compose in order (e.g. spread across AZ, then binpack on memory within
each AZ). This is a real, tunable placement policy engine.

### 2.2 The signals ECS placement can see

This is the single most important finding in this document, because it defines
what is actually novel about Nimbus.

The [cluster query language](https://docs.aws.amazon.com/AmazonECS/latest/developerguide/cluster-query-language.html)
enumerates every subject a placement constraint may evaluate:

```
agentConnected        agent connection status
agentVersion          ECS agent version
attribute:{{name}}    built-in or custom instance attributes
ec2InstanceId         instance id
registeredAt          instance registration timestamp
runningTasksCount     number of running tasks on the instance
task:group            task-group affinity / anti-affinity
```

**There is no subject for observed CPU utilization or observed memory
utilization.** ECS placement reasons about:

- **declared** resource requirements from the task definition (CPU units, memory),
- **reserved vs. remaining** capacity accounting on each container instance,
- **cardinality** (`runningTasksCount`),
- **static/custom attributes** the operator sets.

`binpack` minimising "unused CPU or memory" is reservation arithmetic. A task that
declares 1024 CPU units but idles at 3% still consumes 1024 units of the
placement budget; a task that declares 256 units but pegs a core at 100% is
accounted at 256. Custom attributes are static metadata, not a telemetry channel —
you *could* write a daemon that rewrites custom attributes from `psutil` data, but
that is precisely the experiment Nimbus is running, done by hand.

### 2.3 Other capacity options

- **ECS Managed Instances**: does not support placement strategies; instance
  selection is driven by `instanceRequirements` in the capacity provider.
- **Fargate**: strategies and constraints are not supported; ECS spreads across AZs.

---

## 3. What SQS already solves

[Visibility timeout](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-visibility-timeout.html)
and [dead-letter queues](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-dead-letter-queues.html)
give us, for free, most of the reliability machinery a job queue needs:

- **Lease semantics.** A received message becomes invisible for the visibility
  timeout (default 30 s, max 12 h from first receipt). `ChangeMessageVisibility`
  extends or terminates the lease (set to `0` to release immediately).
- **Crash recovery.** "If you don't process and delete a message before the
  visibility timeout expires — due to application errors, crashes, or connectivity
  problems — the message becomes visible again in the queue."
- **Heartbeating is the documented pattern** for long jobs: "Implement a heartbeat
  mechanism to periodically extend the visibility timeout."
- **At-least-once delivery.** Standard queues do "not guarantee that a message
  won't be delivered more than once within the visibility timeout period" →
  **consumers must be idempotent**. This constrains Nimbus's design (§13 of the
  brief) and is not optional.
- **Bounded retries + DLQ.** `maxReceiveCount` in the redrive policy moves poison
  messages to a DLQ; `redriveAllowPolicy` controls which sources may use it. DLQ
  retention should exceed source retention because standard-queue expiry uses the
  *original* enqueue timestamp.
- **In-flight cap.** ~120,000 in-flight messages for standard queues.
- **Fair queues** (introduced July 2025) mitigate noisy-neighbour effects in
  multi-tenant standard queues by monitoring the distribution of *in-flight*
  messages per `MessageGroupId` and reordering delivery to equalise dwell time —
  with no consumer changes.
  ([announcement](https://aws.amazon.com/about-aws/whats-new/2025/07/amazon-sqs-introduces-fair/),
  [docs](https://docs.aws.amazon.com/AWSSimpleQueueService/latest/SQSDeveloperGuide/sqs-fair-queues.html))

Nimbus will **use** SQS for these properties rather than reimplement them. The
`LocalJobQueue` abstraction (§25 of the brief) deliberately models SQS semantics —
lease, extend, ack, nack, `receive_count`, DLQ — so that behaviour observed
locally transfers.

---

## 4. What EC2 Auto Scaling already solves

[Scaling your Auto Scaling group](https://docs.aws.amazon.com/autoscaling/ec2/userguide/scale-your-group.html)
provides manual, scheduled, dynamic (simple / step / target-tracking) and
**predictive** scaling, plus health checks and instance replacement, default
instance warmup and cooldowns.

Relevance to Nimbus: autoscaling changes the *number* of workers. Nimbus's
question is about *choice among existing workers*. These are complementary, and
Nimbus's fixed three-worker pool is a deliberate experimental control — a variable
fleet size would confound every latency measurement. Nimbus does not autoscale,
and does not claim to improve on autoscaling.

---

## 5. What Nimbus is experimenting with

The hypothesis, stated as a hypothesis:

> For heterogeneous workloads running on workers with dynamically changing
> resource utilization, scheduling decisions based on real-time telemetry and
> workload characteristics **may** improve job completion latency, worker
> utilization and load balance compared with simple FIFO or static priority
> scheduling.

Concretely, four things Nimbus does that the AWS-native mechanisms above do not
do out of the box:

1. **Placement conditioned on observed host telemetry.** Sampled CPU%, memory%,
   run-queue trend, in-process active job count — collected by an agent on each
   worker every 2–5 s — enter the placement decision directly, alongside declared
   requirements. ECS placement has no such subject (§2.2); Batch fair-share uses
   *consumed queue share per tenant*, not *host pressure*.
2. **Weights conditioned on workload class.** The same fleet state produces a
   different ranking for a `CPU_BOUND` job than for a `MEMORY_BOUND` one, because
   the scoring weight vector is selected by class. ECS lets you binpack on `cpu`
   *or* `memory` per strategy element; it does not vary that per job class from a
   learned/declared workload taxonomy.
3. **Per-(worker, job_type) historical runtime as a placement term.** Nimbus keeps
   an EWMA of observed execution time per worker per job type and uses the
   estimate as a scoring term, so a worker that is empirically slow at a workload
   is deprioritised for it.
4. **Decision traces as a first-class, queryable artifact.** Every placement
   records all candidate scores, the per-term contributions, the weight vector,
   the telemetry snapshot it used, and a human-readable reason. This is an
   *explainability and research* artifact, not something the managed services
   expose.

And one thing that is arguably the real deliverable:

5. **A controlled comparison harness.** The same deterministic workload, the same
   fleet, the same measurement code, run through FIFO / Priority / Static
   Resource-Aware / Adaptive — with fixed seeds and repeated trials. The output is
   evidence, in either direction.

---

## 6. Where Nimbus overlaps with AWS

Honest accounting. Nimbus reimplements, in simplified form:

| Capability | AWS-native | Nimbus |
|---|---|---|
| FIFO ordering | Batch default queue strategy | Scheduler A |
| Priority ordering | Batch job priority | Scheduler B |
| Resource-fit filtering | ECS step 1 (CPU/memory/ports) | Scheduler C admission filter |
| Bin-packing / spreading | ECS `binpack` / `spread` | Scheduler C, and the queue term in D |
| Lease + retry + DLQ | SQS visibility timeout, `maxReceiveCount` | `JobQueue` abstraction (SQS-backed in Phase 5) |
| Health-based exclusion | ECS `agentConnected`, ASG health checks | Heartbeat expiry → `UNHEALTHY` |
| Bounded retries | Batch retry strategy | `max_attempts` + attempt counter |
| Fairness across tenants | Batch fair-share, SQS fair queues | *not implemented* — starvation control only |
| Capacity/instance selection | Batch allocation strategies | *not implemented* — fixed fleet |
| Autoscaling | EC2 Auto Scaling | *not implemented* — fixed fleet |
| DAG dependencies | Batch `dependsOn` | *not implemented* |

The last four rows are areas where AWS is strictly more capable and Nimbus makes
no attempt to compete.

---

## 7. Where Nimbus differs

The difference is narrow and should be stated narrowly:

**Signal.** AWS-native placement consumes *declared* requirements, *reserved*
capacity, *static* attributes, and *tenant-level consumption history*. Nimbus adds
*observed, high-frequency, per-host resource telemetry* and *per-(host, job-type)
runtime history* as direct inputs to the per-job placement function.

**Granularity.** Nimbus makes a fresh scoring decision per job against a telemetry
snapshot at most `telemetry_interval` old. ECS placement strategy evaluation is
per-task too, but over reservation state that changes only on task start/stop.

**Observability of the decision.** Nimbus persists the score vector and reason.

That is the whole claim. Everything else Nimbus does is either (a) table stakes it
must build to run the experiment at all, or (b) a simplified version of something
AWS does better.

---

## 8. Why Nimbus is an experimental scheduler, not an AWS replacement

1. **The hypothesis might be false, and that is an acceptable outcome.** Telemetry
   is a *lagging* signal: CPU% reflects the last sampling window, so a scheduler
   that reacts to it can herd — several jobs dispatched inside one telemetry
   interval all pick the same "idle" worker. Reservation-based accounting, which
   is what ECS uses, is immune to that failure mode by construction. It is
   entirely possible that Scheduler C beats Scheduler D on bursty arrivals. The
   benchmark framework is built to detect that, not to hide it.
2. **Nimbus cannot match AWS on control-plane properties it is not building**:
   multi-AZ durability, capacity-aware instance acquisition, Spot interruption
   handling, IAM-grained multi-tenancy, DAG dependencies, or the operational track
   record of a managed service.
3. **A fair head-to-head against Batch is expensive and confounded** (§1.4:
   `RUNNABLE` queueing and `STARTING` image-pull dominate short-job wall clock).
   Where the comparison cannot be made fairly within budget, `docs/experiments.md`
   records it as an **architectural** comparison and reports no fabricated number.
4. **The contribution is measurement, not a product.** The deliverable is: a
   reproducible harness, a decision trace format, and a defensible answer to "does
   telemetry-aware placement help, on which workloads, and by how much."

---

## 9. Claims this project is forbidden from making

Recorded here so they can be checked against the final report:

- ❌ "AWS doesn't have workload scheduling."
- ❌ "Nimbus replaces AWS Batch."
- ❌ "Nimbus is faster than AWS Batch" — without a documented, fair, measured
  experiment, including the confounds in §1.4.
- ❌ Any performance number not produced by `benchmark/` from a recorded run with
  a fixed seed and an archived raw-results file.
- ❌ "AWS scheduling is static" — Batch fair-share is history-aware (§1.2) and SQS
  fair queues are dynamic (§3).

Permitted:

- ✅ "ECS task placement constraints cannot express observed host CPU/memory
  utilization" — supported by the cluster query language subject list (§2.2).
- ✅ "On workload profile X, with fleet Y, Nimbus Adaptive reduced p95 completion
  time by Z% versus FIFO, over N trials, seed S" — when measured.
- ✅ "The hypothesis was not supported on workload profile X" — when measured.
