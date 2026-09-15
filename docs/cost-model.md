# Nimbus Cost Model

All unit prices below are **us-east-1, on-demand, USD**, retrieved from the
[AWS Price List Bulk API](https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/)
on **2026-08-21**, except where marked. `scripts/refresh_prices.py` re-pulls them;
`benchmark/analysis.py` reads `infra/prices.json` so the cost table in any report
is derived, never typed by hand.

> Prices change. Every cost figure in the final report carries the date of the
> price file it was computed from.

---

## 1. Unit prices (retrieved 2026-08-21)

### Compute — EC2 on-demand, Linux

| Instance | vCPU | Memory | $/hour | $/month (730 h) |
|---|---|---|---|---|
| `t3.micro` | 2 | 1 GiB | 0.0104 | 7.59 |
| `t3.small` | 2 | 2 GiB | **0.0208** | 15.18 |
| `t3.medium` | 2 | 4 GiB | 0.0416 | 30.37 |
| `t4g.small` | 2 | 2 GiB | 0.0168 | 12.26 |
| `c6i.large` | 2 | 4 GiB | 0.0850 | 62.05 |
| `c7g.large` | 2 | 4 GiB | 0.0725 | 52.93 |
| `m6i.large` | 2 | 8 GiB | 0.0960 | 70.08 |

**Chosen:** `t3.small` × 4 (3 workers + 1 control plane) = **$0.0832/hour**.
`t3` burstable credits are a measurement hazard (`experiments.md` §8) — if a
headline run exhausts credits it is discarded and re-run on `c6i.large`
($0.34/hour for 4), which is the budgeted fallback.

Storage: EBS `gp3` at **$0.08/GB-month** *(published list price; the EC2 price
list is ~1 GB and is not pulled by `refresh_prices.py` — this is the one figure
entered manually and it is flagged as such)*. 4 × 8 GiB = 32 GiB = **$2.56/month**.

### Queue — Amazon SQS

| Item | Price |
|---|---|
| First 1,000,000 requests/month | **free** |
| Standard queue requests | **$0.40 per million** |
| FIFO queue requests | $0.50 per million |
| Fair queue requests | $0.10 per million |

*(Nimbus uses a standard queue — FIFO's ordering guarantee is irrelevant when the
scheduler reorders anyway, and its throughput cap is a needless constraint.)*

### State — DynamoDB on-demand

| Item | Price |
|---|---|
| Write request units | **$0.625 per million** |
| Read request units | **$0.125 per million** |
| Storage | first 25 GB free, then **$0.25/GB-month** |

### Artifacts — S3 Standard

| Item | Price |
|---|---|
| Storage (first 50 TB) | **$0.023/GB-month** |
| PUT/COPY/POST/LIST | **$0.005 per 1,000** |
| GET and all others | **$0.0004 per 1,000** ($0.004 per 10,000) |

### Observability — CloudWatch

| Item | Price |
|---|---|
| Custom metrics (first 10,000) | **$0.30 per metric-month** |
| API requests (`PutMetricData`, `GetMetricData`) | **$0.01 per 1,000** |
| Logs ingestion (Standard class) | **$0.50/GB** |
| Alarms (standard resolution) | **$0.10 per alarm-month** |

---

## 2. Per-job resource consumption

Derived from the design, to be **replaced by measured counters** from
`raw.jsonl` once Phase 5 runs. Each row is an engineering estimate and is
labelled as such until measured.

| Resource | Units per job | Where |
|---|---|---|
| SQS requests | ~4 (send, receive, 1 visibility extend, delete) + share of empty long-polls | `architecture.md` §3.3 |
| DynamoDB writes | ~7 (job meta ×5 state transitions, 1 attempt, 1 decision) | §7.1 |
| DynamoDB reads | ~3 (job fetch, runtime stats, dashboard) | §7.1 |
| S3 PUTs | 2 (output + stdout); 0 for jobs with inline payloads | §9 |
| S3 GETs | ~1 | §9 |
| CloudWatch datums | ~6, batched 20/call ⇒ ~0.3 API calls | §10 |
| Log bytes | ~2 KB | §10 |

Telemetry, which is **per-second, not per-job**: 3 workers at a 2 s interval =
1.5 writes/s = 129,600 DynamoDB writes/day if persisted at full rate. That alone
is $0.081/day — larger than the entire per-job cost at these volumes. **Mitigation:
persist telemetry samples at 1/10th the scheduling rate (20 s) and keep the 2 s
samples in memory only**, with a 24-hour TTL on what is persisted. The scheduler
reads telemetry from memory anyway (§7.1), so this costs nothing operationally.
This is a design decision driven by the cost model, and it is the reason the cost
model exists before Phase 5 rather than after.

---

## 3. Cost per job batch (marginal, excluding fleet time)

| | 100 jobs | 1,000 jobs |
|---|---|---|
| SQS (400 / 4,000 req) | $0.00016 | $0.00160 |
| DynamoDB writes (700 / 7,000) | $0.00044 | $0.00438 |
| DynamoDB reads (300 / 3,000) | $0.00004 | $0.00038 |
| S3 PUT (200 / 2,000) | $0.00100 | $0.01000 |
| S3 GET (100 / 1,000) | $0.00004 | $0.00040 |
| CloudWatch API (30 / 300 calls) | $0.00030 | $0.00300 |
| CloudWatch Logs (0.2 / 2 MB) | $0.00010 | $0.00100 |
| **Marginal total** | **≈ $0.0021** | **≈ $0.0208** |

**The marginal per-job cost is ~$0.00002 — effectively zero.** Two consequences,
both of which shape how results are reported:

1. **The real cost is fleet time.** At $0.0832/hour, a 100-job run taking 4
   minutes costs $0.0055 in EC2 versus $0.0021 in everything else. Cost is
   therefore ≈ makespan, and cost-per-job is ≈ a restatement of throughput —
   which `experiments.md` §7 requires the report to say out loud rather than
   presenting as an independent finding.
2. **Free-tier absorbs most of it.** 1M SQS requests, 25 GB DynamoDB storage and
   the CloudWatch free tier cover the entire project's non-compute usage.

### Cost per 1000 jobs, including fleet

Assuming a 500-job run at throughput T jobs/s on 4 × `t3.small`:

```
fleet_cost = 0.0832 × (1000 / T) / 3600
cost_per_1000 = fleet_cost + 0.0208
```

| Throughput | Fleet cost / 1000 jobs | Total / 1000 jobs |
|---|---|---|
| 1.0 jobs/s | $0.0231 | $0.0439 |
| 2.0 jobs/s | $0.0116 | $0.0324 |
| 3.0 jobs/s | $0.0077 | $0.0285 |

A strategy that raises throughput from 2.0 to 3.0 jobs/s cuts total cost per 1000
jobs by ~12%. That is the meaningful cost claim, and it is entirely mediated by
makespan.

---

## 4. Project budget

| Item | Assumption | Cost |
|---|---|---|
| 4 × `t3.small` | 40 hours of active experimentation | $3.33 |
| EBS 32 GiB | 1 month | $2.56 |
| SQS / DynamoDB / S3 / CloudWatch | all experiments | < $1.00 |
| CloudWatch alarms | 5 alarms × 1 month | $0.50 |
| Data transfer | intra-AZ, minimal | ~$0 |
| **Nimbus subtotal** | | **≈ $7.50** |
| AWS Batch baseline (Phase 8) | 6 vCPU warm ≈ 3 × `t3.small`, 8 h | $0.50 |
| ECR storage + pulls | 1 image, < 1 GB | ~$0.10 |
| **Total** | | **≈ $8.10** |

**Cost controls, non-negotiable:**

- Local-first: Phases 1–4 and 6–7 cost **$0**.
- **No NAT gateway** ($0.045/hour + $0.045/GB — it would be 55% of the entire
  budget). Use VPC gateway endpoints for S3/DynamoDB and interface endpoints for
  SQS, or place workers in a public subnet with no ingress and a locked security
  group.
- `terraform destroy` at the end of every demo session; `scripts/demo/teardown.sh`.
- AWS Budgets alarm at $15 with an email action.
- On-demand DynamoDB (no idle provisioned capacity), 7-day CloudWatch Logs
  retention, no S3 versioning.
- Single AZ. Multi-AZ would double instance-hours and buy nothing for a
  single-region experiment — an explicit availability trade recorded in
  `limitations.md`.

---

## 5. What the cost analysis will report

1. Measured cost per 100 / 1000 jobs per strategy, from **counted** API calls,
   not estimates.
2. The compute / storage / queue / API split.
3. **Performance per dollar**: jobs per dollar, and p95 latency per dollar-hour.
4. If the Batch baseline runs: its cost per 1000 jobs, noting that Batch adds no
   service charge of its own — you pay for the underlying EC2 — so the comparison
   is really *placement efficiency at equal fleet cost*, plus the operational cost
   Nimbus does not pay because a managed service absorbs it.
5. An explicit statement that **at this scale infrastructure cost is dominated by
   fleet-hours**, so cost differences between strategies are a restatement of
   makespan and must not be presented as an independent result.
