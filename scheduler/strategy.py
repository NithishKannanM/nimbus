"""Composition of an ordering policy and a placement policy, plus trace assembly.

``SchedulingStrategy`` is the object the engine holds. It owns two things the
policies deliberately do not:

1. the full ``FleetSnapshot``, including observed telemetry;
2. the decision trace.

The policy is handed only its declared projection of the snapshot. Whatever
telemetry ends up in the trace is written here, after the policy has already
returned its answer -- so "telemetry was recorded" and "telemetry was consumed"
are separated by construction, not by a comment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

from backend.models.decision import (
    CandidateRecord,
    JobRequirementsRecord,
    SchedulingDecision,
)
from backend.models.job import Job, utcnow
from backend.models.worker import FleetSnapshot, WorkerState
from scheduler.base import OrderingPolicy, PlacementPolicy
from scheduler.ordering import effective_priority
from scheduler.views import job_requirements, telemetry_is_fresh


@dataclass
class SchedulingStrategy:
    name: str
    ordering: OrderingPolicy
    placement: PlacementPolicy
    #: Recorded in every trace so an analysis can tell an A/B control apart.
    telemetry_enabled: bool = False
    #: Freshness threshold used when annotating traces. Applied to *every*
    #: strategy, not just telemetry-aware ones, so any archived trace can be
    #: re-scored under any policy.
    telemetry_max_age_s: float = 6.0

    # -- ordering ---------------------------------------------------------
    def select_next(self, jobs: Sequence[Job], now: datetime) -> Job | None:
        return self.ordering.select_job(jobs, now)

    # -- placement --------------------------------------------------------
    def place(
        self, job: Job, fleet: FleetSnapshot, now: datetime | None = None
    ) -> SchedulingDecision:
        now = now or utcnow()
        requirements = job_requirements(job)

        # The policy's entire information budget is decided by this call.
        started = time.perf_counter_ns()
        views = self.placement.build_views(fleet.workers)
        outcome = self.placement.select_worker(requirements, views, now)
        latency_us = (time.perf_counter_ns() - started) / 1000.0

        return self._trace(job, requirements, fleet, outcome, now, latency_us)

    # -- trace assembly ---------------------------------------------------
    def _trace(
        self, job, requirements, fleet: FleetSnapshot, outcome, now, latency_us
    ) -> SchedulingDecision:
        candidates: dict[str, CandidateRecord] = {}
        for state in fleet.workers:
            candidates[state.worker_id] = self._candidate(
                state, outcome, self.telemetry_max_age_s
            )

        runner_up, margin = self._runner_up(outcome)

        return SchedulingDecision(
            job_id=job.job_id,
            # A decision authorises the *next* attempt: the engine increments
            # Job.attempts when it transitions QUEUED -> SCHEDULED, so recording
            # job.attempts here would label the first placement "attempt 0".
            attempt=job.attempts + 1,
            strategy=self.name,
            algorithm_version=self.placement.algorithm_version,
            ordering_policy=self.ordering.name,
            placement_policy=self.placement.name,
            workload_class=job.workload_class,
            priority=job.priority,
            effective_priority=effective_priority(job, now, self.ordering.aging_interval_s),
            job=JobRequirementsRecord(
                job_type=requirements.job_type,
                workload_class=requirements.workload_class,
                priority=requirements.priority,
                cpu_units=requirements.cpu_units,
                memory_mb=requirements.memory_mb,
                attempts=requirements.attempts,
            ),
            decided_at=now,
            decision_latency_us=latency_us,
            fleet_snapshot_at=fleet.taken_at,
            telemetry_enabled=self.telemetry_enabled,
            weights=outcome.weights,
            candidates=candidates,
            excluded=dict(outcome.excluded),
            selected_worker=outcome.selected_worker,
            runner_up=runner_up,
            margin=margin,
            reason=outcome.reason,
        )

    @staticmethod
    def _candidate(state: WorkerState, outcome, max_age_s: float) -> CandidateRecord:
        worker_id = state.worker_id
        eligible = worker_id in outcome.scores
        return CandidateRecord(
            eligible=eligible,
            ineligible_reason=outcome.excluded.get(worker_id),
            healthy=state.healthy,
            declared_cpu_units=state.declared.cpu_units,
            declared_memory_mb=state.declared.memory_mb,
            declared_slots=state.declared.slots,
            reserved_cpu_units=state.reserved.cpu_units,
            reserved_memory_mb=state.reserved.memory_mb,
            active_jobs=state.reserved.active_jobs,
            # Recorded here, in the strategy, from the full snapshot. The policy
            # that produced `outcome` never had access to this.
            observed=state.observed,
            observed_window=state.observed_window,
            observed_age_s=state.telemetry_age_s,
            telemetry_fresh=telemetry_is_fresh(state, max_age_s),
            score=outcome.scores.get(worker_id),
            terms=outcome.terms.get(worker_id, {}),
        )

    @staticmethod
    def _runner_up(outcome) -> tuple[str | None, float | None]:
        """The candidate that would have won had the selected one been absent.

        Ranked in the policy's own direction, so ``margin`` is always
        non-negative and always means "how much better was the winner".
        """
        selected = outcome.selected_worker
        if selected is None or len(outcome.scores) < 2:
            return None, None
        maximising = getattr(outcome, "objective", "min") == "max"
        others = sorted(
            ((w, s) for w, s in outcome.scores.items() if w != selected),
            key=lambda item: -item[1] if maximising else item[1],
        )
        if not others:
            return None, None
        runner_up, runner_up_score = others[0]
        selected_score = outcome.scores[selected]
        margin = (
            selected_score - runner_up_score if maximising
            else runner_up_score - selected_score
        )
        return runner_up, margin
