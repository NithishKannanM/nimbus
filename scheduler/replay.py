"""Replay archived scheduling decisions.

A decision trace records everything a placement policy was given, so a decision
can be re-executed long after the run finished. That buys three things:

1. **Verification.** Re-running a static policy over a trace must reproduce the
   recorded choice exactly. If it does not, the policy consumed something that
   was not recorded -- which is precisely the failure mode the telemetry
   boundary exists to prevent.

2. **A correct statement of determinism.** Placement is a deterministic function
   of ``(job requirements, static fleet state)``. It is *not* true that two live
   runs of the same workload produce the same placement sequence: reservation
   state at decision time depends on real completion timing, so consecutive runs
   diverge whether telemetry is on or off. Replay tests the property that
   actually holds.

3. **Counterfactuals.** Phase 3 can re-score archived traces under a different
   policy without re-running the workload.

Only scored policies are replayable. Round-robin carries a rotation cursor, so
its choice depends on history the trace does not capture.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.models.decision import SchedulingDecision
from scheduler.base import PlacementOutcome, PlacementPolicy
from scheduler.placement.adaptive import AdaptivePlacement
from scheduler.placement.static_resource_aware import StaticResourceAwarePlacement
from scheduler.views import AdaptiveWorkerView, JobRequirements, StaticWorkerView


class NotReplayable(Exception):
    """The recorded policy depends on state the trace does not capture."""


#: Policies whose decisions are a pure function of what a trace records,
#: keyed by ``placement_policy`` name.
REPLAYABLE = {
    "static_resource_aware": lambda: StaticResourceAwarePlacement(mode="balance"),
    "static_resource_aware_binpack": lambda: StaticResourceAwarePlacement(mode="binpack"),
    "adaptive": lambda: AdaptivePlacement(),
}

#: Which view builder a recorded policy needs.
_ADAPTIVE_POLICIES = frozenset({"adaptive"})

#: Absolute tolerance when comparing replayed scores with recorded ones.
#: Scores are sums of five products of IEEE-754 doubles evaluated in a fixed
#: order, so exact equality is expected; the tolerance covers JSON round-trips
#: of archived traces rather than arithmetic drift.
DEFAULT_TOLERANCE = 1e-9


def static_views(decision: SchedulingDecision) -> tuple[StaticWorkerView, ...]:
    """Rebuild the exact static views the policy was handed."""
    return tuple(
        StaticWorkerView(
            worker_id=worker_id,
            healthy=candidate.healthy,
            cpu_capacity=candidate.declared_cpu_units,
            memory_capacity=candidate.declared_memory_mb,
            cpu_reserved=candidate.reserved_cpu_units,
            memory_reserved=candidate.reserved_memory_mb,
            slots_total=candidate.declared_slots,
            slots_used=candidate.active_jobs,
        )
        for worker_id, candidate in sorted(decision.candidates.items())
    )


def adaptive_views(decision: SchedulingDecision) -> tuple[AdaptiveWorkerView, ...]:
    """Rebuild the exact adaptive views the policy was handed.

    ``telemetry_fresh`` is taken from the trace rather than recomputed from the
    age and the current threshold. The recorded verdict is what the policy
    actually acted on, so replaying it reproduces the real decision even if
    ``NIMBUS_TELEMETRY_MAX_AGE_SECONDS`` has since changed.
    """
    views = []
    for worker_id, candidate in sorted(decision.candidates.items()):
        window = candidate.observed_window
        observed = candidate.observed
        views.append(
            AdaptiveWorkerView(
                worker_id=worker_id,
                healthy=candidate.healthy,
                cpu_capacity=candidate.declared_cpu_units,
                memory_capacity=candidate.declared_memory_mb,
                slots_total=candidate.declared_slots,
                cpu_reserved=candidate.reserved_cpu_units,
                memory_reserved=candidate.reserved_memory_mb,
                slots_used=candidate.active_jobs,
                cpu_percent=window.cpu_percent_mean if window else 0.0,
                cpu_percent_latest=window.cpu_percent_latest if window else 0.0,
                cpu_trend_pct_per_s=window.cpu_trend_pct_per_s if window else 0.0,
                memory_percent=observed.memory_percent if observed else 0.0,
                available_memory_mb=observed.available_memory_mb if observed else 0.0,
                process_memory_mb=observed.process_memory_mb if observed else 0.0,
                load_average=observed.load_average_1m if observed else None,
                telemetry_age_s=candidate.observed_age_s,
                telemetry_samples=window.samples if window else 0,
                telemetry_fresh=candidate.telemetry_fresh,
            )
        )
    return tuple(views)


def views_for(decision: SchedulingDecision):
    """Rebuild whichever view type the recorded policy consumed."""
    if decision.placement_policy in _ADAPTIVE_POLICIES:
        return adaptive_views(decision)
    return static_views(decision)


def requirements(decision: SchedulingDecision) -> JobRequirements:
    if decision.job is None:
        raise NotReplayable(f"{decision.decision_id} has no recorded job requirements")
    return JobRequirements(
        job_id=decision.job_id,
        job_type=decision.job.job_type,
        workload_class=decision.job.workload_class,
        priority=decision.job.priority,
        cpu_units=decision.job.cpu_units,
        memory_mb=decision.job.memory_mb,
        attempts=decision.job.attempts,
    )


def policy_for(decision: SchedulingDecision) -> PlacementPolicy:
    try:
        return REPLAYABLE[decision.placement_policy]()
    except KeyError:
        raise NotReplayable(
            f"{decision.placement_policy!r} is stateful and cannot be replayed from a trace"
        ) from None


def replay(
    decision: SchedulingDecision, policy: PlacementPolicy | None = None
) -> PlacementOutcome:
    """Re-execute a recorded decision from its trace.

    With no ``policy`` argument this reproduces the original. Passing a
    different policy runs a counterfactual: what would *this* algorithm have
    chosen, given exactly the state the original saw?
    """
    target = policy or policy_for(decision)
    if policy is None:
        views = views_for(decision)
    elif isinstance(target, AdaptivePlacement):
        views = adaptive_views(decision)
    else:
        views = static_views(decision)
    return target.select_worker(requirements(decision), views, decision.decided_at)


@dataclass(frozen=True)
class ReplayResult:
    decision_id: str
    job_id: str
    recorded_worker: str | None
    replayed_worker: str | None
    scores_match: bool
    algorithm_version: str = "unversioned"

    @property
    def matches(self) -> bool:
        return self.recorded_worker == self.replayed_worker and self.scores_match


def verify(
    decision: SchedulingDecision,
    policy: PlacementPolicy | None = None,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> ReplayResult:
    outcome = replay(decision, policy)
    recorded = {w: c.score for w, c in decision.candidates.items() if c.score is not None}
    scores_match = set(recorded) == set(outcome.scores) and all(
        abs(recorded[w] - outcome.scores[w]) <= tolerance for w in recorded
    )
    return ReplayResult(
        decision_id=decision.decision_id,
        job_id=decision.job_id,
        recorded_worker=decision.selected_worker,
        replayed_worker=outcome.selected_worker,
        scores_match=scores_match,
        algorithm_version=decision.algorithm_version,
    )


def verify_all(
    decisions, policy: PlacementPolicy | None = None
) -> tuple[list[ReplayResult], list[ReplayResult]]:
    """Returns (matching, mismatching)."""
    results = [verify(d, policy) for d in decisions]
    return [r for r in results if r.matches], [r for r in results if not r.matches]
