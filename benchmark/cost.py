"""Cost derivation from archived runs.

Everything here is a **projection**, never a measurement, and the distinction is
enforced rather than left to the reader: no AWS resources were ever provisioned
(``docs/aws-batch-baseline.md``), so there is no bill to compare against. Every
figure this module produces carries ``measured: False`` and the date of the
price list it came from.

``docs/cost-model.md`` §5 states the conclusion this module exists to make
checkable:

> At this scale infrastructure cost is dominated by fleet-hours, so cost
> differences between strategies are a restatement of makespan and must not be
> presented as an independent result.

That statement is about the *project's* total spend, where the fleet runs for
forty hours and compute swamps everything else. It is not automatically true of
a single ten-second benchmark run, where the per-job API charges are a large
share of a very small total -- and conflating the two is easy, so
:func:`is_restatement_of_makespan` tests the claim that actually matters: that
the *variation* between strategies is pure makespan, because every other term is
identical across arms by construction. A cost table that ranked strategies the
same way the makespan table does, presented as corroboration, would be
double-counting one measurement.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

#: us-east-1 on-demand, USD, retrieved 2026-08-21 from the AWS Price List Bulk
#: API. Copied from docs/cost-model.md section 1, which is the authority; the
#: date travels with every figure derived from them because prices change and a
#: cost figure without its price date is not reproducible.
PRICES_RETRIEVED = "2026-08-21"

INSTANCE_HOURLY_USD: dict[str, float] = {
    "t3.micro": 0.0104,
    "t3.small": 0.0208,
    "t3.medium": 0.0416,
    "t4g.small": 0.0168,
    "c6i.large": 0.0850,
    "c7g.large": 0.0725,
    "m6i.large": 0.0960,
}

#: EBS gp3, $/GB-month. The one figure in cost-model.md entered by hand rather
#: than pulled from the price list, and flagged as such there.
EBS_GP3_GB_MONTH_USD = 0.08

#: SQS standard queue, $ per million requests, after the 1M/month free tier.
SQS_PER_MILLION_REQUESTS_USD = 0.40
#: DynamoDB on-demand, $ per million request units.
DYNAMODB_WRITE_PER_MILLION_USD = 0.625
DYNAMODB_READ_PER_MILLION_USD = 0.125
#: S3 Standard.
S3_PUT_PER_1000_USD = 0.005
S3_GET_PER_1000_USD = 0.0004

HOURS_PER_MONTH = 730.0


@dataclass(frozen=True)
class RunCost:
    """Projected cost of one archived run.

    ``measured`` is always False and is a field rather than a docstring note so
    that anything consuming this -- a report, a dashboard, a spreadsheet --
    carries the caveat with the number instead of beside it.
    """

    run_id: str
    strategy: str
    makespan_s: float
    jobs_completed: int
    fleet_size: int
    instance_type: str

    compute_usd: float
    queue_usd: float
    state_usd: float
    artifacts_usd: float
    total_usd: float

    usd_per_1000_jobs: float
    jobs_per_usd: float

    measured: bool = False
    prices_retrieved: str = PRICES_RETRIEVED

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_run(
    *,
    run_id: str,
    strategy: str,
    makespan_s: float,
    jobs_completed: int,
    decisions: int,
    fleet_size: int = 3,
    instance_type: str = "t3.small",
    control_plane_instances: int = 1,
) -> RunCost:
    """Project one run's AWS cost from its archived metrics and list prices.

    The per-job API-call counts are derived from the system's actual behaviour
    rather than guessed:

    * **SQS** -- at minimum one send, one receive and one delete per job, plus
      a visibility change per lease extension. Four is the floor for a job that
      never retries and never gets deferred, so this understates a contended
      run and is labelled a floor rather than an estimate.
    * **DynamoDB** -- one write for the job record plus one per state
      transition (QUEUED -> SCHEDULED -> RUNNING -> COMPLETED is three), and one
      write per scheduling decision. Reads are dominated by the reaper's
      per-status queries rather than by the hot path, which is served from the
      in-memory registry precisely so that placement latency does not include a
      network round-trip.
    * **S3** -- five archive objects per run, plus nothing per job at these
      payload sizes, since payloads under 256 KB stay inline.
    """
    hours = max(makespan_s, 0.0) / 3600.0
    instances = fleet_size + control_plane_instances
    hourly = INSTANCE_HOURLY_USD.get(instance_type, INSTANCE_HOURLY_USD["t3.small"])
    compute = instances * hourly * hours

    sqs_requests = jobs_completed * 4
    queue = sqs_requests / 1e6 * SQS_PER_MILLION_REQUESTS_USD

    dynamo_writes = jobs_completed * 4 + decisions
    dynamo_reads = jobs_completed * 2
    state = (
        dynamo_writes / 1e6 * DYNAMODB_WRITE_PER_MILLION_USD
        + dynamo_reads / 1e6 * DYNAMODB_READ_PER_MILLION_USD
    )

    artifacts = 5 / 1000 * S3_PUT_PER_1000_USD

    total = compute + queue + state + artifacts
    per_1000 = (total / jobs_completed * 1000.0) if jobs_completed else 0.0

    return RunCost(
        run_id=run_id,
        strategy=strategy,
        makespan_s=round(makespan_s, 3),
        jobs_completed=jobs_completed,
        fleet_size=fleet_size,
        instance_type=instance_type,
        compute_usd=round(compute, 8),
        queue_usd=round(queue, 8),
        state_usd=round(state, 8),
        artifacts_usd=round(artifacts, 8),
        total_usd=round(total, 8),
        usd_per_1000_jobs=round(per_1000, 6),
        jobs_per_usd=round(jobs_completed / total, 1) if total > 0 else 0.0,
        measured=False,
    )


def compute_share(cost: RunCost) -> float:
    """Fraction of projected cost that is fleet-hours.

    The number that decides whether a cost comparison says anything a makespan
    comparison did not.
    """
    return cost.compute_usd / cost.total_usd if cost.total_usd else 0.0


def is_restatement_of_makespan(costs: Iterable[RunCost]) -> bool:
    """Whether ranking strategies by cost reproduces ranking them by makespan.

    Tested directly, by comparing the two orderings, rather than inferred from a
    threshold. Two earlier framings were both wrong and worth recording so they
    are not reinvented:

    * **Compute share of the total.** The wrong question. At benchmark scale a
      run lasts ten seconds, so compute is a minority of a very small per-run
      total -- the check said "not a restatement" while the cost table went on
      ranking strategies in exactly makespan order.
    * **Identical non-compute charges across arms.** Nearly right, but false in
      detail: decision counts differ between strategies (a policy that defers
      more produces more decisions, hence more DynamoDB writes), so the
      per-job charges are close but not equal.

    What the report actually needs to know is whether the cost table is telling
    a reader anything the makespan table did not. That is a question about the
    two orderings, so this compares them.
    """
    costs = list(costs)
    by_strategy: dict[str, list[RunCost]] = {}
    for cost in costs:
        by_strategy.setdefault(cost.strategy, []).append(cost)
    if len(by_strategy) < 2:
        return False

    import statistics

    by_cost = sorted(
        by_strategy,
        key=lambda s: statistics.median(c.usd_per_1000_jobs for c in by_strategy[s]),
    )
    by_makespan = sorted(
        by_strategy,
        key=lambda s: statistics.median(c.makespan_s for c in by_strategy[s]),
    )
    return by_cost == by_makespan


def monthly_idle_usd(
    *, fleet_size: int = 3, instance_type: str = "t3.small", root_volume_gb: int = 8
) -> dict[str, float]:
    """What the stack costs per month if nobody turns it off.

    Included because it is the number the budget actually depends on. The
    project's whole spend is ~$8 (docs/cost-model.md §4) and a forgotten fleet
    exceeds that in under four days -- which is why `terraform destroy` is
    listed there as a non-negotiable control rather than as tidy-up.
    """
    instances = fleet_size + 1
    hourly = INSTANCE_HOURLY_USD.get(instance_type, INSTANCE_HOURLY_USD["t3.small"])
    compute = instances * hourly * HOURS_PER_MONTH
    storage = instances * root_volume_gb * EBS_GP3_GB_MONTH_USD
    return {
        "compute_usd": round(compute, 2),
        "storage_usd": round(storage, 2),
        "total_usd": round(compute + storage, 2),
        "days_to_exceed_8_usd": round(8.0 / ((compute + storage) / 30.0), 2),
    }


def summarise(costs: list[RunCost]) -> dict[str, Any]:
    """Per-strategy medians, plus the compute-domination verdict."""
    import statistics
    from collections import defaultdict

    by_strategy: dict[str, list[RunCost]] = defaultdict(list)
    for cost in costs:
        by_strategy[cost.strategy].append(cost)

    return {
        "prices_retrieved": PRICES_RETRIEVED,
        "measured": False,
        "note": (
            "Projected from list prices. No AWS resources were provisioned, so "
            "there is no bill to validate these against "
            "(docs/aws-batch-baseline.md)."
        ),
        "variation_is_makespan": is_restatement_of_makespan(costs),
        "min_compute_share": round(min((compute_share(c) for c in costs), default=0.0), 6),
        "max_compute_share": round(max((compute_share(c) for c in costs), default=0.0), 6),
        "by_strategy": {
            strategy: {
                "n": len(runs),
                "median_usd_per_1000_jobs": round(
                    statistics.median([r.usd_per_1000_jobs for r in runs]), 6
                ),
                "median_jobs_per_usd": round(
                    statistics.median([r.jobs_per_usd for r in runs]), 1
                ),
                "median_makespan_s": round(
                    statistics.median([r.makespan_s for r in runs]), 3
                ),
            }
            for strategy, runs in sorted(by_strategy.items())
        },
    }
