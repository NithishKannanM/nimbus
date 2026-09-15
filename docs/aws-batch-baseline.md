# Phase 8 — AWS Batch baseline: a documented refusal

**Status: no Batch performance numbers are reported.** This document is the
disclosure that `docs/experiments.md` §6.3 requires, written in the form it
specifies.

> A fair quantitative comparison with AWS Batch was not achievable within the
> project's budget and time constraints because **no AWS resources were ever
> provisioned**: the entire project ran locally, `terraform apply` was never
> executed, and consequently there is no Batch compute environment, no Nimbus
> EC2 fleet, and no pair of runs on matched hardware to compare. What follows is
> an architectural comparison only. No Batch performance numbers are reported.

This is the outcome the roadmap explicitly permits — *"AWS Batch baseline, **or
a documented refusal to publish an unfair one**"* — and it is recorded here as a
result rather than as an omission.

---

## 1. Why not just run it anyway

Because the number would be wrong in a specific, predictable, self-serving
direction, and it would look fine.

`docs/aws-native-scheduling.md` §1.4 documents that a Batch job can remain in
`RUNNABLE` indefinitely while capacity is found, and that the `STARTING`
transition absorbs the full container image pull. The Nimbus workloads run for
0.1–40 s. A cold Batch environment would therefore spend more time provisioning
than the jobs spend computing, and a wall-clock comparison would report Nimbus
beating AWS Batch by a large factor.

That result would be **entirely an artefact of measuring cold start**, and it
would be indistinguishable, in the write-up, from a genuine scheduling result.
It is exactly risk **R4** in `docs/architecture.md` §14, whose stated impact is
"dishonest headline".

A project whose central claim is that it reports findings honestly — including
the finding that its own pre-registered success criterion failed — cannot then
publish a comparison it knows to be rigged in its favour.

---

## 2. The seven conditions, and their status

`docs/experiments.md` §6.2: *all of these, or no number.*

| # | Condition | Status | Note |
|---|---|---|---|
| 1 | Compute environment pre-scaled to `minvCpus` = 6, warm before the run | ❌ not met | `infra/batch.tf` is **written** and sets `min_vcpus = 6`, but was never applied |
| 2 | Container image pre-pulled onto the instances | ❌ not met | No image was ever built or pushed; no ECR repository exists |
| 3 | Identical workload code, same entry point | ⚠️ designed, unbuilt | `batch.tf` invokes `worker.batch_entrypoint` against the same closed workload registry; that entry point is not implemented |
| 4 | Identical instance types and count | ❌ not met | Neither fleet exists |
| 5 | Same seeded job sequence and arrival process | ✅ available | `benchmark/generator.py` is deterministic in `(profile, seed, job_count)` and would drive both |
| 6 | Measurement from Batch `startedAt`/`stoppedAt` **and** separately end-to-end from `createdAt` | ⚠️ designed, unrun | The dual-measurement protocol is specified; no runs exist to apply it to |
| 7 | ≥5 trials | ❌ not met | Zero trials |

Four conditions unmet, two designed but unexercised. **One** is satisfied. The
rule is *all of these, or no number*, so: no number.

## 3. What would be needed to lift this refusal

Concretely, and in order. None of it is research; it is all execution.

1. `terraform apply` with `enable_batch_baseline = true` and a `batch_image` URI.
   Cost: the warm compute environment is the expensive line — 6 vCPUs held at
   `minVcpus > 0` for the duration, roughly `$0.06/hour` on `t3.small`, budgeted
   at `$0.50` for an 8-hour window (`docs/cost-model.md` §4).
2. Build and push a container image running the Nimbus workload registry, and
   implement `worker/batch_entrypoint.py` as a thin `run_workload` shim. It must
   import the *same* module the Nimbus worker executes, or the comparison is
   between two programs rather than between two schedulers.
3. Pre-pull the image onto the warm instances and confirm it before submitting.
4. Submit the identical seeded job sequence to both systems, ≥5 trials,
   interleaved.
5. Report both measurement windows with the difference explained, per §6.2(6).

Until all five are done, this document stands.

## 4. What is compared regardless

The architectural comparison does not depend on any of the above, and it is
already written: `docs/aws-native-scheduling.md` §6–§7 covers signal sources,
decision granularity, explainability, and — importantly — the capability matrix
showing the many rows where **AWS Batch is strictly more capable than Nimbus**.

The short version, restated here so this document is self-contained:

| Dimension | AWS Batch | Nimbus |
|---|---|---|
| Placement signal | Declared vCPU/memory requirements; fair-share consumption per tenant | Declared + reserved + **observed host telemetry** and per-`(host, job_type)` runtime history |
| Dependency DAGs | `dependsOn`, N-to-N and array dependencies | **None** |
| Fair-share scheduling | Decay-weighted historical usage per share identifier | **None** |
| Capacity management | Managed compute environments, Spot, allocation strategies | Fixed 3-worker fleet, no autoscaling |
| Multi-tenancy, quotas, IAM per job | Yes | **None** — single-tenant, unauthenticated (`docs/limitations.md` §5) |
| Retry with exit-code matching | Yes | Flat retry budget with exponential backoff |
| Per-decision explainability | Not exposed | Full candidate set, scores, exclusion reasons, runner-up and margin, **replayable** |
| Scheduling latency | Not exposed | Measured per decision, published as `decision_latency_us` |

Nimbus is more capable on exactly two rows: the placement signal, and the
explainability of a single decision. Those two rows are the entire research
question. Every other row is one where a production system should use Batch.

## 5. The honest form of the claim

What Phase 3 measured is that **adding observed host telemetry to a placement
function changes placement, and under external CPU pressure that change reduces
makespan** — measured against Nimbus's own baselines, on one machine, in one
regime, as boundary-condition evidence (`docs/results.md`).

What it did **not** measure, and what nothing in this project supports:

- that Nimbus outperforms AWS Batch;
- that Nimbus outperforms ECS task placement;
- that observed telemetry would help a *production* scheduler, which faces
  multi-tenancy, capacity acquisition, and dependency graphs that Nimbus does
  not implement at all;
- anything at all about behaviour on EC2, since no run ever happened there.

The narrow claim is the only one the evidence carries, and inflating it with an
unfair Batch comparison would cost more credibility than the comparison could
possibly buy.
