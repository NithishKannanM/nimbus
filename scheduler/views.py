"""Restricted views handed to placement policies.

This module is the experimental boundary, expressed structurally.

The Phase 2 / Phase 3 question is whether *observed* host telemetry carries
placement information beyond *declared* capacity and Nimbus's own
*reservations*. That comparison is only valid if the static baseline genuinely
cannot see telemetry. Relying on a policy author's discipline is not good
enough: one stray ``worker.observed.cpu_percent`` and the baseline quietly
becomes telemetry-aware, the experiment silently loses its control, and nothing
fails.

So the static policy is not given a worker object with telemetry it promises
not to read. It is given ``StaticWorkerView``, which has no telemetry fields at
all. Reading one is an ``AttributeError``, not a judgement call.

``test_views.py`` pins the exact field set, so adding an observed field to this
view fails the suite.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Protocol, runtime_checkable

from backend.models.enums import WorkloadClass
from backend.models.job import Job
from backend.models.worker import WorkerState


@runtime_checkable
class HasStaticFields(Protocol):
    """The DECLARED + RESERVED fields every placement view must expose.

    Both ``StaticWorkerView`` and ``AdaptiveWorkerView`` satisfy this, which is
    what lets one feasibility filter serve both. Feasibility is evaluated on
    static fields only, in exactly one place, for every policy -- telemetry can
    influence ranking but must never make an infeasible worker eligible.
    """

    worker_id: str
    healthy: bool
    cpu_capacity: int
    memory_capacity: int
    cpu_reserved: int
    memory_reserved: int
    slots_total: int
    slots_used: int


@dataclass(frozen=True, slots=True)
class StaticWorkerView:
    """DECLARED + RESERVED only. No OBSERVED anything.

    ``slots=True`` matters: it removes ``__dict__``, so no code can attach
    telemetry to an instance at runtime either.
    """

    worker_id: str
    healthy: bool
    cpu_capacity: int
    memory_capacity: int
    cpu_reserved: int
    memory_reserved: int
    slots_total: int
    slots_used: int

    # -- derived, still purely static ------------------------------------
    @property
    def cpu_available(self) -> int:
        return self.cpu_capacity - self.cpu_reserved

    @property
    def memory_available(self) -> int:
        return self.memory_capacity - self.memory_reserved

    @property
    def slots_free(self) -> int:
        return max(0, self.slots_total - self.slots_used)

    def projected_cpu_utilisation(self, cpu_units: int) -> float:
        if self.cpu_capacity <= 0:
            return 1.0
        return (self.cpu_reserved + cpu_units) / self.cpu_capacity

    def projected_memory_utilisation(self, memory_mb: int) -> float:
        if self.memory_capacity <= 0:
            return 1.0
        return (self.memory_reserved + memory_mb) / self.memory_capacity

    def projected_slot_utilisation(self) -> float:
        if self.slots_total <= 0:
            return 1.0
        return (self.slots_used + 1) / self.slots_total


@dataclass(frozen=True, slots=True)
class AdaptiveWorkerView:
    """DECLARED + RESERVED + OBSERVED, plus telemetry quality.

    The information budget of a telemetry-aware policy, made explicit. It is
    still a *projection*, not the internal ``WorkerState``: no mutable worker
    internals, no history buffers, no registry handle. A policy holding one of
    these can read observed metrics and can tell how stale they are -- and
    nothing else.

    The DECLARED and RESERVED field names deliberately match
    ``StaticWorkerView`` so both satisfy ``HasStaticFields`` and share one
    feasibility filter, rather than duplicating that logic.
    """

    worker_id: str
    healthy: bool

    # -- DECLARED ---------------------------------------------------------
    cpu_capacity: int
    memory_capacity: int
    slots_total: int

    # -- RESERVED ---------------------------------------------------------
    cpu_reserved: int
    memory_reserved: int
    slots_used: int

    # -- OBSERVED ---------------------------------------------------------
    cpu_percent: float
    """Smoothed over the pre-registered window, not the latest raw sample."""
    cpu_percent_latest: float
    cpu_trend_pct_per_s: float
    memory_percent: float
    available_memory_mb: float
    process_memory_mb: float
    load_average: float | None

    # -- TELEMETRY QUALITY ------------------------------------------------
    telemetry_age_s: float | None
    telemetry_samples: int
    telemetry_fresh: bool
    """False when telemetry is missing, unsampled, or older than the
    configured maximum age. A policy must degrade deterministically rather
    than read a stale number as current truth."""

    # -- derived, static --------------------------------------------------
    @property
    def cpu_available(self) -> int:
        return self.cpu_capacity - self.cpu_reserved

    @property
    def memory_available(self) -> int:
        return self.memory_capacity - self.memory_reserved

    @property
    def slots_free(self) -> int:
        return max(0, self.slots_total - self.slots_used)

    def projected_cpu_utilisation(self, cpu_units: int) -> float:
        if self.cpu_capacity <= 0:
            return 1.0
        return (self.cpu_reserved + cpu_units) / self.cpu_capacity

    def projected_memory_utilisation(self, memory_mb: int) -> float:
        if self.memory_capacity <= 0:
            return 1.0
        return (self.memory_reserved + memory_mb) / self.memory_capacity

    def projected_slot_utilisation(self) -> float:
        if self.slots_total <= 0:
            return 1.0
        return (self.slots_used + 1) / self.slots_total


@dataclass(frozen=True, slots=True)
class JobRequirements:
    """What a placement policy may know about a job.

    Declared requirements and workload class only -- no payload, no result, no
    execution history. Historical runtime becomes an Adaptive input in Phase 3
    and is deliberately absent here.
    """

    job_id: str
    job_type: str
    workload_class: WorkloadClass
    priority: int
    cpu_units: int
    memory_mb: int
    attempts: int


#: The exact field set of StaticWorkerView, pinned by test_views.py.
STATIC_VIEW_FIELDS: frozenset[str] = frozenset(f.name for f in fields(StaticWorkerView))

#: The exact field set of AdaptiveWorkerView, pinned by test_views.py.
ADAPTIVE_VIEW_FIELDS: frozenset[str] = frozenset(f.name for f in fields(AdaptiveWorkerView))

#: Substrings that must never appear in a static view's field names.
OBSERVED_FIELD_TOKENS: tuple[str, ...] = (
    "observed", "percent", "telemetry", "load_average", "trend", "psutil", "usage",
)


def static_view(state: WorkerState) -> StaticWorkerView:
    """Project a full WorkerState down to what static placement may see.

    ``healthy`` comes from heartbeat liveness, not from telemetry: liveness and
    resource observation are separate signals, and excluding a dead worker is a
    correctness requirement for every strategy, not an experimental variable.
    """
    return StaticWorkerView(
        worker_id=state.worker_id,
        healthy=state.healthy,
        cpu_capacity=state.declared.cpu_units,
        memory_capacity=state.declared.memory_mb,
        cpu_reserved=state.reserved.cpu_units,
        memory_reserved=state.reserved.memory_mb,
        slots_total=state.declared.slots,
        slots_used=state.reserved.active_jobs,
    )


def static_fleet_view(workers: tuple[WorkerState, ...]) -> tuple[StaticWorkerView, ...]:
    return tuple(static_view(w) for w in workers)


def job_requirements(job: Job) -> JobRequirements:
    return JobRequirements(
        job_id=job.job_id,
        job_type=job.job_type,
        workload_class=job.workload_class,
        priority=job.priority,
        cpu_units=job.cpu_units,
        memory_mb=job.memory_mb,
        attempts=job.attempts,
    )


def telemetry_is_fresh(state: WorkerState, max_age_s: float) -> bool:
    """The single definition of "trustworthy telemetry".

    Used both by ``adaptive_view`` (so the policy sees it) and by decision-trace
    assembly (so *every* trace records it, whichever policy ran). Recording it
    uniformly is what lets an archived static trace be re-scored under adaptive
    counterfactually -- without it, a static trace would look as though every
    worker had stale telemetry.
    """
    window = state.observed_window
    return (
        state.observed is not None
        and window is not None
        and window.samples > 0
        and state.telemetry_age_s is not None
        and state.telemetry_age_s <= max_age_s
    )


def adaptive_view(state: WorkerState, *, max_age_s: float) -> AdaptiveWorkerView:
    """Project a WorkerState down to what a telemetry-aware policy may see.

    Telemetry quality is resolved here, once, so a policy never has to decide
    for itself whether a reading is trustworthy. A worker is *fresh* only if it
    has a sample, a non-empty smoothing window, a known age, and that age is
    within ``max_age_s``.

    When telemetry is absent the observed fields are zero-filled, but
    ``telemetry_fresh=False`` travels with them: a policy that reads a zero
    without checking the flag is reading a value this projection never claimed
    was real. adaptive-v1 substitutes reservation-derived estimates instead
    (docs/adaptive-v1-preregistration.md section 3).
    """
    observed = state.observed
    window = state.observed_window
    age = state.telemetry_age_s
    samples = window.samples if window else 0
    fresh = telemetry_is_fresh(state, max_age_s)
    return AdaptiveWorkerView(
        worker_id=state.worker_id,
        healthy=state.healthy,
        cpu_capacity=state.declared.cpu_units,
        memory_capacity=state.declared.memory_mb,
        slots_total=state.declared.slots,
        cpu_reserved=state.reserved.cpu_units,
        memory_reserved=state.reserved.memory_mb,
        slots_used=state.reserved.active_jobs,
        cpu_percent=window.cpu_percent_mean if window else 0.0,
        cpu_percent_latest=window.cpu_percent_latest if window else 0.0,
        cpu_trend_pct_per_s=window.cpu_trend_pct_per_s if window else 0.0,
        memory_percent=observed.memory_percent if observed else 0.0,
        available_memory_mb=observed.available_memory_mb if observed else 0.0,
        process_memory_mb=observed.process_memory_mb if observed else 0.0,
        load_average=observed.load_average_1m if observed else None,
        telemetry_age_s=age,
        telemetry_samples=samples,
        telemetry_fresh=fresh,
    )


def adaptive_fleet_view(
    workers: tuple[WorkerState, ...], *, max_age_s: float
) -> tuple[AdaptiveWorkerView, ...]:
    return tuple(adaptive_view(w, max_age_s=max_age_s) for w in workers)
