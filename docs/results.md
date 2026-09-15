# Nimbus — results

Generated 2026-08-31T14:56:00+00:00 by `scripts/generate_report.py` from `.nimbus/artifacts/benchmarks`. Every figure below is derived from the archive; nothing is typed in by hand. Re-running the command regenerates this file.

**157 archived runs** across 5 profiles.

---

## Hypothesis

> For heterogeneous workloads on workers with dynamically changing resource utilisation, do scheduling decisions based on real-time telemetry improve completion latency, utilisation and load balance compared with simpler strategies?

The real test is not *does adaptive beat FIFO* — it should, almost arithmetically. It is **does adaptive beat Static Resource-Aware**: does live telemetry add anything over the reservation accounting ECS already does.

## Per-profile results

### Profile `burst`

20 archived run(s); 9 pass the §6.3 load-regime gate.

**Evidence class: inadmissible.** Most runs fall below the §6.3 load-regime gate, and this profile applies no external pressure — so this is genuinely low load, not the gate mis-firing. In that regime the static baseline degenerates into its `worker_id` tie-break (R9) and any advantage `adaptive` shows would be for a reason unrelated to telemetry. Figures below cover the admissible runs only and are reported for completeness, not as evidence.

| strategy | n | median makespan_s | IQR | range |
|---|---:|---:|---|---|
| `adaptive` | 2 | 12.9727 | 12.9727–12.9727 | 12.9674–12.978 |
| `fifo` | 3 | 13.2773 | 13.2773–13.2773 | 13.1462–13.3171 |
| `static_resource_aware` | 2 | 12.9877 | 12.9877–12.9877 | 12.9744–13.0011 |
| `static_resource_aware_binpack` | 2 | 12.9872 | 12.9872–12.9872 | 12.9777–12.9967 |

Mann-Whitney U, two-sided, Bonferroni-corrected across 3 comparisons (α = 0.0167).

| comparison | median Δ | p | Cliff's δ | effect | significant | ranges disjoint |
|---|---:|---:|---:|---|---|---|
| adaptive vs `fifo` | -2.3% | 0.2000 | -1.00 | large | no | yes |
| adaptive vs `static_resource_aware` | -0.1% | 0.6667 | -0.50 | large | no | no |
| adaptive vs `static_resource_aware_binpack` | -0.1% | 0.6667 | -0.50 | large | no | no |

Placements decided by the `worker_id` tie-break rather than by the policy's own scoring:

| strategy | tie-break share |
|---|---:|
| `adaptive` | 1.7% |
| `fifo` | 0.0% |
| `static_resource_aware` | 20.3% |
| `static_resource_aware_binpack` | 14.3% |

Where a static policy ties often and `adaptive` does not, part of any measured advantage is *not tying* rather than telemetry content. `fifo` never ties, which makes **adaptive vs `fifo`** the comparison free of this confound.

<details><summary>11 run(s) excluded from primary evidence</summary>

- exp-3085f052 (fifo): mean fleet utilisation 0.4242 < 0.50, R9 low-load confound
- exp-24de13f0 (static_resource_aware): mean fleet utilisation 0.4611 < 0.50, R9 low-load confound
- exp-e5327a2b (adaptive): mean fleet utilisation 0.4964 < 0.50, R9 low-load confound
- exp-670f1c9c (static_resource_aware): mean fleet utilisation 0.4896 < 0.50, R9 low-load confound
- exp-ec12cffd (static_resource_aware_binpack): mean fleet utilisation 0.4990 < 0.50, R9 low-load confound
- exp-07566640 (static_resource_aware_binpack): mean fleet utilisation 0.4858 < 0.50, R9 low-load confound
- exp-521b7baf (adaptive): mean fleet utilisation 0.4110 < 0.50, R9 low-load confound
- exp-9de4661b (fifo): mean fleet utilisation 0.4840 < 0.50, R9 low-load confound
- exp-46e68fa4 (static_resource_aware): mean fleet utilisation 0.4674 < 0.50, R9 low-load confound
- exp-6c1b4b21 (adaptive): mean fleet utilisation 0.4402 < 0.50, R9 low-load confound
- exp-c4533b1f (static_resource_aware_binpack): mean fleet utilisation 0.4699 < 0.50, R9 low-load confound

</details>

### Profile `cpu_pressure`

54 archived run(s); 0 pass the §6.3 load-regime gate.

**Evidence class: descriptive.** The load gate excludes most of this profile's runs, which on an external-pressure profile is the mis-fire recorded in `docs/adaptive-v1-preregistration.md` §7: a scheduler that successfully steers work away from a saturated worker leaves that worker idle *from Nimbus's point of view*, which lowers measured fleet utilisation. The gate then excludes exactly the runs where the effect is largest. Figures below are computed over all runs and are boundary-condition evidence, not primary evidence for H3.

| strategy | n | median makespan_s | IQR | range |
|---|---:|---:|---|---|
| `adaptive` | 13 | 11.8747 | 11.6195–31.0566 | 11.3231–32.8123 |
| `fifo` | 14 | 37.3104 | 30.991–45.5951 | 11.3774–46.1555 |
| `static_resource_aware` | 14 | 14.6328 | 12.6111–44.1429 | 11.0785–54.4384 |
| `static_resource_aware_binpack` | 13 | 45.5373 | 31.9783–46.1599 | 11.4238–46.6804 |

Mann-Whitney U, two-sided, Bonferroni-corrected across 3 comparisons (α = 0.0167).

| comparison | median Δ | p | Cliff's δ | effect | significant | ranges disjoint |
|---|---:|---:|---:|---|---|---|
| adaptive vs `fifo` | -68.2% | 0.0013 | -0.70 | large | yes | no |
| adaptive vs `static_resource_aware` | -18.9% | 0.0543 | -0.44 | medium | no | no |
| adaptive vs `static_resource_aware_binpack` | -73.9% | 0.0008 | -0.74 | large | yes | no |

Placements decided by the `worker_id` tie-break rather than by the policy's own scoring:

| strategy | tie-break share |
|---|---:|
| `adaptive` | 1.0% |
| `fifo` | 0.0% |
| `static_resource_aware` | 25.5% |
| `static_resource_aware_binpack` | 22.7% |

Where a static policy ties often and `adaptive` does not, part of any measured advantage is *not tying* rather than telemetry content. `fifo` never ties, which makes **adaptive vs `fifo`** the comparison free of this confound.

<details><summary>54 run(s) excluded from primary evidence</summary>

- exp-9bc1189a (fifo): mean fleet utilisation 0.2888 < 0.50, R9 low-load confound
- exp-3adb53fe (static_resource_aware): mean fleet utilisation 0.2665 < 0.50, R9 low-load confound
- exp-3623a2ef (adaptive): mean fleet utilisation 0.2570 < 0.50, R9 low-load confound
- exp-d1a5963e (static_resource_aware_binpack): mean fleet utilisation 0.2725 < 0.50, R9 low-load confound
- exp-afc896aa (static_resource_aware): mean fleet utilisation 0.3924 < 0.50, R9 low-load confound
- exp-01b0ecc4 (adaptive): mean fleet utilisation 0.2617 < 0.50, R9 low-load confound
- exp-129302ca (static_resource_aware_binpack): mean fleet utilisation 0.2694 < 0.50, R9 low-load confound
- exp-4816dadc (fifo): mean fleet utilisation 0.2443 < 0.50, R9 low-load confound
- exp-66e39ecf (adaptive): mean fleet utilisation 0.3884 < 0.50, R9 low-load confound
- exp-939310d2 (static_resource_aware_binpack): truncated, 59/60 completed
- exp-9df09133 (fifo): mean fleet utilisation 0.2955 < 0.50, R9 low-load confound
- exp-9eeb3beb (static_resource_aware): truncated, 59/60 completed
- exp-ebe808b0 (static_resource_aware_binpack): mean fleet utilisation 0.2734 < 0.50, R9 low-load confound
- exp-8b4cf6d5 (fifo): mean fleet utilisation 0.4550 < 0.50, R9 low-load confound
- exp-30ccf506 (static_resource_aware): mean fleet utilisation 0.4526 < 0.50, R9 low-load confound
- exp-63c5cc67 (adaptive): mean fleet utilisation 0.3789 < 0.50, R9 low-load confound
- exp-5d0fef71 (fifo): mean fleet utilisation 0.2545 < 0.50, R9 low-load confound
- exp-6e986c74 (static_resource_aware): truncated, 59/60 completed
- exp-1bb99d01 (adaptive): mean fleet utilisation 0.2930 < 0.50, R9 low-load confound
- exp-f08788d2 (static_resource_aware_binpack): mean fleet utilisation 0.2510 < 0.50, R9 low-load confound
- exp-a6f7cbcc (fifo): mean fleet utilisation 0.2439 < 0.50, R9 low-load confound
- exp-b12b0fa7 (static_resource_aware): mean fleet utilisation 0.2932 < 0.50, R9 low-load confound
- exp-e5c5e67c (adaptive): mean fleet utilisation 0.3857 < 0.50, R9 low-load confound
- exp-265e26ca (static_resource_aware_binpack): mean fleet utilisation 0.2623 < 0.50, R9 low-load confound
- exp-558bc990 (static_resource_aware): mean fleet utilisation 0.4881 < 0.50, R9 low-load confound
- exp-f9381ea1 (adaptive): mean fleet utilisation 0.3748 < 0.50, R9 low-load confound
- exp-04e90176 (static_resource_aware_binpack): mean fleet utilisation 0.2545 < 0.50, R9 low-load confound
- exp-a035cc1f (fifo): mean fleet utilisation 0.4512 < 0.50, R9 low-load confound
- exp-620a1706 (adaptive): mean fleet utilisation 0.4107 < 0.50, R9 low-load confound
- exp-c964fa8c (static_resource_aware_binpack): mean fleet utilisation 0.2611 < 0.50, R9 low-load confound
- exp-415f1313 (fifo): mean fleet utilisation 0.2459 < 0.50, R9 low-load confound
- exp-f0a3a887 (static_resource_aware): mean fleet utilisation 0.4575 < 0.50, R9 low-load confound
- exp-c3ee3fee (static_resource_aware_binpack): mean fleet utilisation 0.2524 < 0.50, R9 low-load confound
- exp-e1e7c1e6 (fifo): mean fleet utilisation 0.4512 < 0.50, R9 low-load confound
- exp-69fedd97 (static_resource_aware): mean fleet utilisation 0.2650 < 0.50, R9 low-load confound
- exp-b0632740 (adaptive): mean fleet utilisation 0.2591 < 0.50, R9 low-load confound
- exp-79648da3 (fifo): mean fleet utilisation 0.2674 < 0.50, R9 low-load confound
- exp-93cdffa4 (static_resource_aware): mean fleet utilisation 0.2380 < 0.50, R9 low-load confound
- exp-cf4fa738 (adaptive): mean fleet utilisation 0.4123 < 0.50, R9 low-load confound
- exp-7dcc440a (static_resource_aware_binpack): mean fleet utilisation 0.2516 < 0.50, R9 low-load confound
- exp-58fcadc7 (static_resource_aware): mean fleet utilisation 0.4053 < 0.50, R9 low-load confound
- exp-7bcf224f (adaptive): mean fleet utilisation 0.4286 < 0.50, R9 low-load confound
- exp-d33ef9c7 (static_resource_aware_binpack): mean fleet utilisation 0.2564 < 0.50, R9 low-load confound
- exp-ba1d7d9f (fifo): mean fleet utilisation 0.2626 < 0.50, R9 low-load confound
- exp-1a4b8daa (adaptive): mean fleet utilisation 0.3939 < 0.50, R9 low-load confound
- exp-a75ae01f (static_resource_aware_binpack): mean fleet utilisation 0.3697 < 0.50, R9 low-load confound
- exp-7f031a04 (fifo): mean fleet utilisation 0.2436 < 0.50, R9 low-load confound
- exp-b54403a6 (static_resource_aware): mean fleet utilisation 0.4538 < 0.50, R9 low-load confound
- exp-11d03f96 (static_resource_aware_binpack): mean fleet utilisation 0.4271 < 0.50, R9 low-load confound
- exp-80e9e97e (fifo): mean fleet utilisation 0.2435 < 0.50, R9 low-load confound
- exp-277d85cf (static_resource_aware): mean fleet utilisation 0.2389 < 0.50, R9 low-load confound
- exp-5d129b10 (adaptive): mean fleet utilisation 0.4402 < 0.50, R9 low-load confound
- exp-8fce5180 (fifo): mean fleet utilisation 0.2627 < 0.50, R9 low-load confound
- exp-91cb7ea2 (static_resource_aware): mean fleet utilisation 0.2434 < 0.50, R9 low-load confound

</details>

### Profile `heavy_tail`

20 archived run(s); 0 pass the §6.3 load-regime gate.

**Evidence class: inadmissible.** Most runs fall below the §6.3 load-regime gate, and this profile applies no external pressure — so this is genuinely low load, not the gate mis-firing. In that regime the static baseline degenerates into its `worker_id` tie-break (R9) and any advantage `adaptive` shows would be for a reason unrelated to telemetry. Figures below cover the admissible runs only and are reported for completeness, not as evidence.

No admissible runs, so no comparison is reported.

### Profile `homogeneous_saturated`

43 archived run(s); 40 pass the §6.3 load-regime gate.

**Evidence class: primary.**

| strategy | n | median makespan_s | IQR | range |
|---|---:|---:|---|---|
| `adaptive` | 10 | 9.4079 | 8.8522–10.3887 | 8.3439–11.2041 |
| `fifo` | 10 | 9.2636 | 8.9102–9.5993 | 8.3317–11.6589 |
| `static_resource_aware` | 10 | 10.2035 | 9.5408–10.3266 | 9.1441–10.7731 |
| `static_resource_aware_binpack` | 10 | 9.8104 | 8.6633–10.2502 | 8.0537–10.9466 |

Mann-Whitney U, two-sided, Bonferroni-corrected across 3 comparisons (α = 0.0167).

| comparison | median Δ | p | Cliff's δ | effect | significant | ranges disjoint |
|---|---:|---:|---:|---|---|---|
| adaptive vs `fifo` | +1.6% | 1.0000 | +0.00 | negligible | no | no |
| adaptive vs `static_resource_aware` | -7.8% | 0.3527 | -0.26 | small | no | no |
| adaptive vs `static_resource_aware_binpack` | -4.1% | 1.0000 | +0.00 | negligible | no | no |

Placements decided by the `worker_id` tie-break rather than by the policy's own scoring:

| strategy | tie-break share |
|---|---:|
| `adaptive` | 10.0% |
| `fifo` | 0.0% |
| `static_resource_aware` | 14.3% |
| `static_resource_aware_binpack` | 7.2% |

Where a static policy ties often and `adaptive` does not, part of any measured advantage is *not tying* rather than telemetry content. `fifo` never ties, which makes **adaptive vs `fifo`** the comparison free of this confound.

<details><summary>3 run(s) excluded from primary evidence</summary>

- exp-747af8b8 (fifo): mean fleet utilisation 0.2461 < 0.50, R9 low-load confound
- exp-05951ed1 (static_resource_aware): mean fleet utilisation 0.3089 < 0.50, R9 low-load confound
- exp-f6826153 (adaptive): mean fleet utilisation 0.3563 < 0.50, R9 low-load confound

</details>

### Profile `memory_pressure`

20 archived run(s); 0 pass the §6.3 load-regime gate.

**Evidence class: descriptive.** The load gate excludes most of this profile's runs, which on an external-pressure profile is the mis-fire recorded in `docs/adaptive-v1-preregistration.md` §7: a scheduler that successfully steers work away from a saturated worker leaves that worker idle *from Nimbus's point of view*, which lowers measured fleet utilisation. The gate then excludes exactly the runs where the effect is largest. Figures below are computed over all runs and are boundary-condition evidence, not primary evidence for H3.

| strategy | n | median makespan_s | IQR | range |
|---|---:|---:|---|---|
| `adaptive` | 5 | 15.8398 | 15.6607–16.5712 | 15.5814–16.7049 |
| `fifo` | 5 | 16.3627 | 16.3207–16.6552 | 16.1515–17.5351 |
| `static_resource_aware` | 5 | 16.1531 | 15.7324–16.4979 | 15.6891–16.5916 |
| `static_resource_aware_binpack` | 5 | 15.9279 | 15.7275–15.9301 | 15.6716–16.3431 |

Mann-Whitney U, two-sided, Bonferroni-corrected across 3 comparisons (α = 0.0167).

| comparison | median Δ | p | Cliff's δ | effect | significant | ranges disjoint |
|---|---:|---:|---:|---|---|---|
| adaptive vs `fifo` | -3.2% | 0.3095 | -0.44 | medium | no | no |
| adaptive vs `static_resource_aware` | -1.9% | 0.8413 | -0.12 | negligible | no | no |
| adaptive vs `static_resource_aware_binpack` | -0.6% | 1.0000 | -0.04 | negligible | no | no |

Placements decided by the `worker_id` tie-break rather than by the policy's own scoring:

| strategy | tie-break share |
|---|---:|
| `adaptive` | 0.7% |
| `fifo` | 0.0% |
| `static_resource_aware` | 53.0% |
| `static_resource_aware_binpack` | 41.0% |

Where a static policy ties often and `adaptive` does not, part of any measured advantage is *not tying* rather than telemetry content. `fifo` never ties, which makes **adaptive vs `fifo`** the comparison free of this confound.

<details><summary>20 run(s) excluded from primary evidence</summary>

- exp-53cf0b4b (fifo): mean fleet utilisation 0.3875 < 0.50, R9 low-load confound
- exp-aef465f9 (static_resource_aware): mean fleet utilisation 0.3314 < 0.50, R9 low-load confound
- exp-9572825e (adaptive): mean fleet utilisation 0.3523 < 0.50, R9 low-load confound
- exp-434814f4 (static_resource_aware_binpack): mean fleet utilisation 0.3488 < 0.50, R9 low-load confound
- exp-2aa2a6f1 (static_resource_aware): mean fleet utilisation 0.4375 < 0.50, R9 low-load confound
- exp-52b8d70b (adaptive): mean fleet utilisation 0.2966 < 0.50, R9 low-load confound
- exp-80c2845e (static_resource_aware_binpack): mean fleet utilisation 0.2404 < 0.50, R9 low-load confound
- exp-4fc55538 (fifo): mean fleet utilisation 0.2944 < 0.50, R9 low-load confound
- exp-9f42d45c (adaptive): mean fleet utilisation 0.2886 < 0.50, R9 low-load confound
- exp-45c1f57c (static_resource_aware_binpack): mean fleet utilisation 0.2632 < 0.50, R9 low-load confound
- exp-d52200fa (fifo): mean fleet utilisation 0.2958 < 0.50, R9 low-load confound
- exp-e039867e (static_resource_aware): mean fleet utilisation 0.2670 < 0.50, R9 low-load confound
- exp-ac530dd5 (static_resource_aware_binpack): mean fleet utilisation 0.2267 < 0.50, R9 low-load confound
- exp-89359237 (fifo): mean fleet utilisation 0.2773 < 0.50, R9 low-load confound
- exp-280a66da (static_resource_aware): mean fleet utilisation 0.2533 < 0.50, R9 low-load confound
- exp-c304581a (adaptive): mean fleet utilisation 0.2657 < 0.50, R9 low-load confound
- exp-14251b32 (fifo): mean fleet utilisation 0.2874 < 0.50, R9 low-load confound
- exp-889a09ab (static_resource_aware): mean fleet utilisation 0.2379 < 0.50, R9 low-load confound
- exp-7f5ef47d (adaptive): mean fleet utilisation 0.2722 < 0.50, R9 low-load confound
- exp-3de03c87 (static_resource_aware_binpack): mean fleet utilisation 0.2064 < 0.50, R9 low-load confound

</details>

---

## Cost

**Projected, not measured.** Prices are us-east-1 on-demand list prices retrieved 2026-08-21 (`docs/cost-model.md` §1). No AWS resources were ever provisioned, so there is no bill to validate these against (`docs/aws-batch-baseline.md`).

| strategy | n | median $/1000 jobs | median jobs/$ | median makespan |
|---|---:|---:|---:|---:|
| `adaptive` | 39 | $0.0099 | 101,345 | 11.6s |
| `fifo` | 40 | $0.0104 | 95,921 | 13.1s |
| `static_resource_aware` | 40 | $0.0103 | 97,442 | 12.6s |
| `static_resource_aware_binpack` | 38 | $0.0104 | 96,270 | 13.0s |

Compute is 29.6%–79.5% of projected cost per run. At benchmark scale a run lasts seconds, so the per-job API charges are a large share of a very small total; the project's *overall* spend is compute-dominated because the fleet runs for hours between runs, not only during them (`docs/cost-model.md` §4).

> **This table is a restatement of makespan, not an independent result.** Ranking the strategies by cost reproduces the makespan ranking exactly — checked, not assumed. Every strategy runs the same seeded job sequence on an identical fleet, so the queue and state charges are near-identical across arms and the term that actually varies is compute: fleet size × hourly price × makespan. `docs/cost-model.md` §5 requires this to be said rather than letting a table that happens to agree with the makespan table read as corroboration. It is one measurement reported twice.

Idle stack, if nobody runs `terraform destroy`: **$63.30/month** ($60.74 compute + $2.56 EBS). That exceeds the project's entire ~$8 budget in **3.8 days**, which is why teardown is listed as a non-negotiable cost control rather than as tidy-up.

---

## Conclusions

Derived from the tables above by `scripts/generate_report.py`, so they cannot drift from the data they describe.

1. **On the profile(s) admissible as primary evidence (`homogeneous_saturated`), no comparison reaches significance.** No strategy beats another on makespan. For a homogeneous fleet under saturation that is the *predicted* outcome and a good one: `docs/adaptive-v1-preregistration.md` §6.4 lists Profile A as "primary — a null result here is a *good* result", because there is little for any scheduler to exploit and what is under test is whether `adaptive` adds overhead or instability when telemetry carries no extra information. It does not.

2. **Under external pressure, `adaptive` reduced makespan substantially — as boundary-condition evidence only.** `cpu_pressure` vs `fifo` -68.2% (p=0.0013, δ=-0.70, large); `cpu_pressure` vs `static_resource_aware_binpack` -73.9% (p=0.0008, δ=-0.74, large). Those profiles are classed descriptive because the §6.3 load gate excludes their runs, which on a pressure profile is the mis-fire recorded in `docs/adaptive-v1-preregistration.md` §7 rather than a sign the runs were too light. Descriptive evidence is not primary evidence, and this is not presented as confirming H3.

3. **Part of any advantage over the static policies is not telemetry.** `static_resource_aware` decides up to 75.3% of placements by the `worker_id` tie-break rather than by its own scoring, while `adaptive` tops out at 10.0% — a continuous observed signal rarely produces exact equality. `fifo` never ties, which makes adaptive-vs-`fifo` the only comparison free of this confound.

4. **The pre-registered materiality criterion failed on 2 comparison(s) that the standing statistical protocol finds significant.** It required non-overlapping per-run ranges, which is near-unsatisfiable on a bimodal distribution at this N regardless of effect size: one unlucky treatment run and one lucky baseline run overlap the ranges while the medians stay far apart. That is a flaw in the criterion, not evidence against the effect — but it was fixed in advance, so it is reported as failing rather than quietly replaced.

5. **Cost differences between strategies are makespan in different units.** All strategies rent an identical fleet, so cost per hour is identical and only the rental duration varies. Not an independent result, and not presented as one.

Profiles `burst`, `heavy_tail` produced no admissible evidence: most of their runs fall below the load-regime gate and they apply no external pressure, so that is genuine low load rather than the gate mis-firing. In that regime the static baseline degenerates into its tie-break, and any apparent win would be for the wrong reason.

## What these results do not support

- **Nothing about AWS Batch or ECS.** No AWS resources were ever provisioned. See `docs/aws-batch-baseline.md` for the refusal to publish an unfair comparison, in the form `docs/experiments.md` §6.3 requires.
- **Nothing about EC2.** These are local runs. Three processes pinned to disjoint cores on one host share a page cache and a memory bus in a way three EC2 instances do not (`docs/limitations.md` §3), so these numbers are directional and the headline experiment needs re-running on real hardware.
- **Nothing about regimes that were not run.** Every statement is bounded by the profiles and load levels in the tables above.
- **Nothing about production scheduling.** Nimbus has no dependency DAGs, no fair-share, no multi-tenancy, no autoscaling and no authentication. It is more capable than AWS Batch on exactly two dimensions — placement signal and per-decision explainability — and those two are the research question.
