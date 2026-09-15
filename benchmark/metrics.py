"""Metric computation for a benchmark run.

Definitions are spelled out because "average completion time" is ambiguous
otherwise, and because the load-balance metric in particular is easy to get
wrong.

Load balance is measured on **time**, not job counts. The Phase 1 baseline made
the reason concrete: a run distributed 16/7/4 jobs across three workers while
their busy times were 2.10 / 2.24 / 2.22 s. By job count that fleet looks badly
imbalanced; by utilisation it is almost perfectly balanced, and utilisation is
what actually determines makespan. Job counts are reported, but only
descriptively.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any, Sequence

from backend.models.decision import SchedulingDecision
from backend.models.enums import JobStatus
from backend.models.job import Job
from backend.models.telemetry import WorkerTelemetry


def _percentile(values: Sequence[float], q: float) -> float:
    """Nearest-rank percentile. Explicit so results do not depend on which
    interpolation method a library happens to default to."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int(round(q * len(ordered) + 0.5))))
    return ordered[rank - 1]


@dataclass(frozen=True)
class LatencyStats:
    n: int
    mean: float
    p50: float
    p95: float
    p99: float
    min: float
    max: float

    @classmethod
    def of(cls, values: Sequence[float]) -> "LatencyStats":
        if not values:
            return cls(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        return cls(
            n=len(values),
            mean=round(statistics.fmean(values), 3),
            p50=round(statistics.median(values), 3),
            p95=round(_percentile(values, 0.95), 3),
            p99=round(_percentile(values, 0.99), 3),
            min=round(min(values), 3),
            max=round(max(values), 3),
        )


@dataclass(frozen=True)
class WorkerUtilisation:
    worker_id: str
    jobs: int
    busy_ms: float
    slots: int
    utilisation: float
    """busy_ms / (makespan_ms * slots) -- fraction of the worker's capacity used."""
    mean_observed_cpu: float | None = None
    max_observed_cpu: float | None = None
    telemetry_samples: int = 0


@dataclass(frozen=True)
class PlacementDynamics:
    """Oscillation instrumentation (adaptive-v1 pre-registration section 5).

    adaptive-v1 deliberately ships without hysteresis or damping. If reacting to
    telemetry makes placement oscillate -- job lands, CPU rises, scheduler flees,
    CPU falls, scheduler floods -- that is a finding to measure and document
    before any stabilisation is added.
    """

    switch_rate: float
    """Fraction of consecutive placements landing on a different worker than the
    previous one. 0 = perfectly sticky. With 3 workers, blind round-robin gives
    1.0 and uniform-random gives ~0.67, so a high value is not by itself bad."""

    concentration_max: float
    """Worst single-worker share within any one telemetry interval. This is the
    direct measure of telemetry-lag herding: several jobs dispatched inside one
    sampling window all see the same stale snapshot."""

    concentration_mean: float
    windows: int

    tie_rate: float = 0.0
    """Fraction of placements where the top two candidates scored identically,
    i.e. the choice was made by the worker_id tie-break rather than by the
    policy.

    Added before any Phase 3 experiment was run, because the pre-registered
    load-regime gate (mean fleet utilisation >= 0.50) is only a *proxy* for the
    R9 confound. R9 is specifically "Static degenerates because scores tie";
    this measures that directly, so a run can be assessed on the actual
    confound rather than on a stand-in for it."""

    mean_margin: float = 0.0
    """Mean gap between the selected worker and the runner-up. Near zero means
    the policy was barely discriminating."""


@dataclass(frozen=True)
class LoadBalance:
    """Primary balance metrics, on utilisation. Job counts are descriptive."""

    utilisation_cv: float
    """Coefficient of variation across workers. 0 = perfect. Scale-free."""
    jain_fairness: float
    """(sum x)^2 / (n * sum x^2), in (0, 1]. Higher = fairer."""
    utilisation_by_worker: dict[str, float]
    busy_ms_by_worker: dict[str, float]
    job_count_by_worker: dict[str, int]
    job_count_cv: float
    """Reported for contrast only. NOT the load-balance metric."""


@dataclass
class RunMetrics:
    jobs_submitted: int
    jobs_completed: int
    jobs_failed: int
    jobs_unfinished: int
    makespan_s: float
    throughput_jobs_s: float

    completion_ms: LatencyStats
    queue_wait_ms: LatencyStats
    execution_ms: LatencyStats
    scheduling_latency_us: LatencyStats

    load_balance: LoadBalance
    mean_fleet_utilisation: float
    """Gate for the R9 low-load confound: primary inferential runs require
    >= 0.50 (docs/adaptive-v1-preregistration.md section 6.3)."""
    placement_dynamics: PlacementDynamics
    workers: list[WorkerUtilisation]

    retry_rate: float
    failure_rate: float
    dead_letters: int
    decisions: int
    unplaced_decisions: int
    telemetry_overhead: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def load_regime_ok(self) -> bool:
        """Whether this run may be used as primary evidence."""
        return self.mean_fleet_utilisation >= 0.50

    def headline(self) -> str:
        return (
            f"completed={self.jobs_completed}/{self.jobs_submitted} "
            f"makespan={self.makespan_s:.2f}s "
            f"throughput={self.throughput_jobs_s:.2f}/s "
            f"completion_p50={self.completion_ms.p50:.0f}ms "
            f"p95={self.completion_ms.p95:.0f}ms "
            f"util_cv={self.load_balance.utilisation_cv:.4f}"
        )


def _cv(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = statistics.fmean(values)
    if mean == 0:
        return 0.0
    return statistics.stdev(values) / mean


def _jain(values: Sequence[float]) -> float:
    if not values:
        return 1.0
    total = sum(values)
    squares = sum(v * v for v in values)
    if squares == 0:
        return 1.0
    return (total * total) / (len(values) * squares)


def placement_dynamics(
    decisions: Sequence[SchedulingDecision], *, window_s: float
) -> PlacementDynamics:
    placed = sorted(
        (d for d in decisions if d.selected_worker),
        key=lambda d: d.decided_at,
    )
    if len(placed) < 2:
        return PlacementDynamics(0.0, 0.0, 0.0, 0)

    switches = sum(
        1
        for previous, current in zip(placed, placed[1:])
        if previous.selected_worker != current.selected_worker
    )
    switch_rate = switches / (len(placed) - 1)

    # Tumbling windows of one telemetry interval, anchored at the first
    # placement: every decision inside one window saw the same telemetry.
    start = placed[0].decided_at
    buckets: dict[int, list[str]] = defaultdict(list)
    for decision in placed:
        offset = (decision.decided_at - start).total_seconds()
        buckets[int(offset // window_s) if window_s > 0 else 0].append(
            decision.selected_worker
        )

    shares = [
        max(Counter(workers).values()) / len(workers)
        for workers in buckets.values()
        if len(workers) >= 2
    ]

    margins = [d.margin for d in placed if d.margin is not None]
    ties = sum(1 for m in margins if m == 0.0)
    return PlacementDynamics(
        switch_rate=round(switch_rate, 6),
        concentration_max=round(max(shares), 6) if shares else 0.0,
        concentration_mean=round(statistics.fmean(shares), 6) if shares else 0.0,
        windows=len(shares),
        tie_rate=round(ties / len(margins), 6) if margins else 0.0,
        mean_margin=round(statistics.fmean(margins), 6) if margins else 0.0,
    )


def summarise(
    jobs: Sequence[Job],
    decisions: Sequence[SchedulingDecision],
    *,
    slots_by_worker: dict[str, int],
    telemetry_by_worker: dict[str, list[WorkerTelemetry]] | None = None,
    dead_letters: int = 0,
    telemetry_overhead: dict[str, float] | None = None,
    telemetry_interval_s: float = 2.0,
) -> RunMetrics:
    telemetry_by_worker = telemetry_by_worker or {}
    completed = [j for j in jobs if j.status is JobStatus.COMPLETED]
    failed = [j for j in jobs if j.status is JobStatus.FAILED]
    unfinished = [j for j in jobs if j.status not in (JobStatus.COMPLETED, JobStatus.FAILED)]

    # Makespan spans first submission to last completion, so it covers queueing
    # as well as execution.
    if completed:
        first_submit = min(j.created_at for j in jobs)
        last_complete = max(j.completed_at for j in completed)
        makespan_s = max((last_complete - first_submit).total_seconds(), 1e-9)
    else:
        makespan_s = 0.0

    completion_ms = [
        (j.completed_at - j.created_at).total_seconds() * 1000.0 for j in completed
    ]
    queue_wait_ms = [
        (j.started_at - j.created_at).total_seconds() * 1000.0
        for j in completed
        if j.started_at is not None
    ]
    execution_ms = [j.execution_time_ms for j in completed if j.execution_time_ms is not None]
    scheduling_us = [d.decision_latency_us for d in decisions]

    busy_by_worker: dict[str, float] = {w: 0.0 for w in slots_by_worker}
    count_by_worker: dict[str, int] = {w: 0 for w in slots_by_worker}
    for job in completed:
        worker = job.assigned_worker
        if worker is None:
            continue
        busy_by_worker.setdefault(worker, 0.0)
        count_by_worker.setdefault(worker, 0)
        busy_by_worker[worker] += job.execution_time_ms or 0.0
        count_by_worker[worker] += 1

    worker_rows: list[WorkerUtilisation] = []
    utilisation_by_worker: dict[str, float] = {}
    for worker_id in sorted(busy_by_worker):
        slots = slots_by_worker.get(worker_id, 1)
        capacity_ms = makespan_s * 1000.0 * slots
        utilisation = busy_by_worker[worker_id] / capacity_ms if capacity_ms > 0 else 0.0
        utilisation_by_worker[worker_id] = round(utilisation, 6)

        samples = telemetry_by_worker.get(worker_id, [])
        cpu_values = [s.cpu_percent for s in samples]
        worker_rows.append(
            WorkerUtilisation(
                worker_id=worker_id,
                jobs=count_by_worker[worker_id],
                busy_ms=round(busy_by_worker[worker_id], 2),
                slots=slots,
                utilisation=round(utilisation, 6),
                mean_observed_cpu=round(statistics.fmean(cpu_values), 2) if cpu_values else None,
                max_observed_cpu=round(max(cpu_values), 2) if cpu_values else None,
                telemetry_samples=len(samples),
            )
        )

    utilisations = list(utilisation_by_worker.values())
    counts = [float(c) for c in count_by_worker.values()]

    attempts_beyond_first = sum(max(0, j.attempts - 1) for j in jobs)
    submitted = len(jobs)

    return RunMetrics(
        jobs_submitted=submitted,
        jobs_completed=len(completed),
        jobs_failed=len(failed),
        jobs_unfinished=len(unfinished),
        makespan_s=round(makespan_s, 4),
        throughput_jobs_s=round(len(completed) / makespan_s, 4) if makespan_s > 0 else 0.0,
        completion_ms=LatencyStats.of(completion_ms),
        queue_wait_ms=LatencyStats.of(queue_wait_ms),
        execution_ms=LatencyStats.of(execution_ms),
        scheduling_latency_us=LatencyStats.of(scheduling_us),
        mean_fleet_utilisation=round(
            statistics.fmean(utilisations) if utilisations else 0.0, 6
        ),
        placement_dynamics=placement_dynamics(
            decisions, window_s=telemetry_interval_s
        ),
        load_balance=LoadBalance(
            utilisation_cv=round(_cv(utilisations), 6),
            jain_fairness=round(_jain(utilisations), 6),
            utilisation_by_worker=utilisation_by_worker,
            busy_ms_by_worker={k: round(v, 2) for k, v in busy_by_worker.items()},
            job_count_by_worker=count_by_worker,
            job_count_cv=round(_cv(counts), 6),
        ),
        workers=worker_rows,
        retry_rate=round(attempts_beyond_first / submitted, 6) if submitted else 0.0,
        failure_rate=round(len(failed) / submitted, 6) if submitted else 0.0,
        dead_letters=dead_letters,
        decisions=len(decisions),
        unplaced_decisions=sum(1 for d in decisions if d.selected_worker is None),
        telemetry_overhead=telemetry_overhead or {},
    )
