"""Ordering and placement policy interfaces, and the shared feasibility filter.

Nimbus keeps two scheduling decisions strictly separate:

    OrderingPolicy.select_job()      which queued job is considered next
    PlacementPolicy.select_worker()  which worker receives it

They are separate objects, not two methods on one class, so that a benchmark
can hold one constant while varying the other. Without that, a comparison
between "FIFO" and "Static Resource-Aware" would vary ordering and placement
simultaneously and could not attribute any difference to either.

A ``PlacementPolicy`` also declares *what it is allowed to see*, by building its
own worker views from the fleet snapshot. That is the enforcement point for the
telemetry boundary described in scheduler/views.py.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Generic, Literal, Sequence, TypeVar

from backend.models.job import Job
from backend.models.worker import WorkerState
from scheduler.views import (
    HasStaticFields,
    JobRequirements,
    StaticWorkerView,
    static_fleet_view,
)

V = TypeVar("V")


# --------------------------------------------------------------------------- #
# ordering
# --------------------------------------------------------------------------- #
class OrderingPolicy(ABC):
    """Decides which queued job is considered next. Sees jobs, never workers."""

    name: str = "base"

    def __init__(self, *, aging_interval_s: float = 30.0) -> None:
        self.aging_interval_s = aging_interval_s

    @abstractmethod
    def select_job(self, jobs: Sequence[Job], now: datetime) -> Job | None: ...


# --------------------------------------------------------------------------- #
# placement
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PlacementOutcome:
    """What a placement policy returns.

    Scores and terms cover the *eligible* candidates only; ``excluded`` records
    why each other worker was filtered out. Both go into the decision trace.
    """

    selected_worker: str | None
    reason: str
    scores: dict[str, float] = field(default_factory=dict)
    terms: dict[str, dict[str, float]] = field(default_factory=dict)
    excluded: dict[str, str] = field(default_factory=dict)
    weights: dict[str, float] | None = None

    objective: Literal["min", "max"] = "min"
    """Which direction wins. Needed so "runner-up" means *the candidate that
    would have been chosen instead* for both minimising policies (balance,
    adaptive) and maximising ones (binpack) -- otherwise binpack reports a
    negative margin against the candidate furthest from its own choice."""


class PlacementPolicy(ABC, Generic[V]):
    """Decides which worker runs a job.

    ``algorithm_version`` is recorded in every decision trace and experiment
    manifest. A changed formula, weight vector, smoothing window or freshness
    policy is a NEW version, never an edit to an existing one -- otherwise a
    later change silently contaminates comparisons against archived results.

    ``build_views`` is how a policy declares its information budget. The engine
    hands over the full fleet snapshot exactly once, here; ``select_worker``
    then only ever sees the projection. A policy that projects to
    ``StaticWorkerView`` cannot read telemetry even by mistake, because the
    objects it holds do not have those attributes.
    """

    name: str = "base"
    algorithm_version: str = "unversioned"

    @abstractmethod
    def build_views(self, workers: Sequence[WorkerState]) -> tuple[V, ...]: ...

    @abstractmethod
    def select_worker(
        self, job: JobRequirements, workers: Sequence[V], now: datetime
    ) -> PlacementOutcome: ...


class StaticPlacementPolicy(PlacementPolicy[StaticWorkerView]):
    """Base for every policy that may use DECLARED and RESERVED state only.

    Subclasses inherit ``build_views`` and must not override it: that single
    method is what keeps observed telemetry out of the static baseline.
    """

    def build_views(self, workers: Sequence[WorkerState]) -> tuple[StaticWorkerView, ...]:
        return static_fleet_view(tuple(workers))


# --------------------------------------------------------------------------- #
# shared feasibility filter
# --------------------------------------------------------------------------- #
def infeasibility_reason(job: JobRequirements, worker: HasStaticFields) -> str | None:
    """Hard constraints. Returns None if the worker is a valid candidate.

    Every policy runs this before ranking; no policy may place onto a worker
    that fails it. Health is included because scheduling onto a worker whose
    heartbeat has expired is a correctness bug for every strategy, not an
    experimental variable.
    """
    if not worker.healthy:
        return "worker not healthy"
    if worker.slots_free <= 0:
        return f"no free slot: {worker.slots_used}/{worker.slots_total}"
    if worker.cpu_available < job.cpu_units:
        return (
            f"insufficient cpu units: need {job.cpu_units}, "
            f"available {worker.cpu_available}"
        )
    if worker.memory_available < job.memory_mb:
        return (
            f"insufficient memory: need {job.memory_mb}MB, "
            f"available {worker.memory_available}MB"
        )
    return None


def partition_candidates(
    job: JobRequirements, workers: Sequence[HasStaticFields]
) -> tuple[list, dict[str, str]]:
    """Split candidates on static feasibility only.

    Shared by every policy, static and adaptive alike. Telemetry influences
    ranking; it never changes eligibility.
    """
    eligible: list = []
    excluded: dict[str, str] = {}
    for worker in workers:
        reason = infeasibility_reason(job, worker)
        if reason is None:
            eligible.append(worker)
        else:
            excluded[worker.worker_id] = reason
    return eligible, excluded
