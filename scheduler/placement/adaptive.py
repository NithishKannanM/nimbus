"""adaptive-v1 — telemetry-aware placement.

The primary experimental scheduler. Everything here was fixed in
``docs/adaptive-v1-preregistration.md`` before any adaptive code existed and
before any Phase 3 experiment ran. It must not be changed in response to
results: an improved algorithm becomes ``adaptive-v2`` and a separate
experiment.

What it is
----------
Static Resource-Aware **plus** observed host state. Not observed state alone:
Nimbus knows about work it has already dispatched that may not yet appear in
telemetry, and discarding that would be strictly worse. The reservation and
queue terms are the Phase 2 static model, carried over unchanged.

The blind spot it targets
-------------------------
Phase 2 measured a positive feedback loop. External pressure slows a worker, so
it *completes* fewer jobs, so its reservation count is the lowest in the fleet,
so a "least projected-loaded" policy reads it as idle and sends it more work.
Low reservation pressure does not imply high execution capacity. adaptive-v1
can see the difference because ``S_cpu`` is measured, not inferred.

Whether that actually helps is the Phase 3 question, and it may not.
"""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

from backend.models.enums import WorkloadClass
from scheduler.base import PlacementOutcome, PlacementPolicy, partition_candidates
from scheduler.views import (
    AdaptiveWorkerView,
    JobRequirements,
    adaptive_fleet_view,
)
from backend.models.worker import WorkerState

ALGORITHM_VERSION = "adaptive-v1"

#: Slope at which the trend term saturates, in percent per second.
TREND_SCALE = 20.0

#: Frozen weight vectors. Sum to 1 per class; every component is in [0, 1], so
#: Score is in [0, 1] and comparable across jobs, workers and runs.
ADAPTIVE_WEIGHTS: dict[WorkloadClass, dict[str, float]] = {
    WorkloadClass.CPU_BOUND: {
        "cpu": 0.50, "memory": 0.10, "reservation": 0.20, "queue": 0.15, "trend": 0.05,
    },
    WorkloadClass.MEMORY_BOUND: {
        "cpu": 0.10, "memory": 0.50, "reservation": 0.20, "queue": 0.15, "trend": 0.05,
    },
    WorkloadClass.IO_BOUND: {
        "cpu": 0.15, "memory": 0.15, "reservation": 0.25, "queue": 0.40, "trend": 0.05,
    },
    WorkloadClass.LIGHTWEIGHT: {
        "cpu": 0.10, "memory": 0.05, "reservation": 0.25, "queue": 0.55, "trend": 0.05,
    },
}

COMPONENTS = ("cpu", "memory", "reservation", "queue", "trend")


def _clip(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


class AdaptivePlacement(PlacementPolicy[AdaptiveWorkerView]):
    name = "adaptive"
    algorithm_version = ALGORITHM_VERSION

    def __init__(self, *, telemetry_max_age_s: float = 6.0) -> None:
        self.telemetry_max_age_s = telemetry_max_age_s

    # -- information budget ----------------------------------------------
    def build_views(self, workers: Sequence[WorkerState]) -> tuple[AdaptiveWorkerView, ...]:
        return adaptive_fleet_view(tuple(workers), max_age_s=self.telemetry_max_age_s)

    # -- scoring ----------------------------------------------------------
    @staticmethod
    def components(job: JobRequirements, worker: AdaptiveWorkerView) -> dict[str, float]:
        """The five pre-registered components, each normalised to [0, 1].

        When telemetry is stale or missing, the OBSERVED components fall back to
        their reservation-derived equivalents. That keeps every candidate on one
        scale, uses Nimbus's best available estimate, and -- critically -- never
        reads an unobserved worker as idle. Treating missing telemetry as 0% CPU
        would make the least-known worker the most attractive one.
        """
        projected_cpu = worker.projected_cpu_utilisation(job.cpu_units)
        projected_memory = worker.projected_memory_utilisation(job.memory_mb)

        if worker.telemetry_fresh:
            observed_cpu = _clip(worker.cpu_percent / 100.0)
            # Host-scoped and worker-scoped memory pressure answer the same
            # question; the larger is the conservative answer. See the
            # pre-registration section 2.2 for the local-fleet caveat.
            host_memory = worker.memory_percent / 100.0
            own_memory = (
                worker.process_memory_mb / worker.memory_capacity
                if worker.memory_capacity > 0
                else 1.0
            )
            observed_memory = _clip(max(host_memory, own_memory))
            trend = _clip(0.5 + worker.cpu_trend_pct_per_s / (2.0 * TREND_SCALE))
        else:
            observed_cpu = _clip(projected_cpu)
            observed_memory = _clip(projected_memory)
            trend = 0.5

        return {
            "cpu": observed_cpu,
            "memory": observed_memory,
            "reservation": _clip(0.5 * projected_cpu + 0.5 * projected_memory),
            "queue": _clip(worker.projected_slot_utilisation()),
            "trend": trend,
        }

    @staticmethod
    def score(components: dict[str, float], weights: dict[str, float]) -> float:
        return sum(weights[name] * components[name] for name in COMPONENTS)

    @staticmethod
    def weights_for(workload_class: WorkloadClass) -> dict[str, float]:
        return dict(ADAPTIVE_WEIGHTS[workload_class])

    # -- placement --------------------------------------------------------
    def select_worker(
        self, job: JobRequirements, workers: Sequence[AdaptiveWorkerView], now: datetime
    ) -> PlacementOutcome:
        weights = self.weights_for(job.workload_class)

        # Feasibility first, on static fields only. Telemetry ranks; it never
        # makes an infeasible worker eligible, and there is no cliff-edge
        # "cpu > 90 means never" rule anywhere in this policy.
        eligible, excluded = partition_candidates(job, workers)
        if not eligible:
            return PlacementOutcome(
                selected_worker=None,
                reason=f"no eligible worker ({len(excluded)} excluded)",
                excluded=excluded,
                weights=weights,
            )

        terms: dict[str, dict[str, float]] = {}
        scores: dict[str, float] = {}
        for worker in eligible:
            components = self.components(job, worker)
            scores[worker.worker_id] = self.score(components, weights)
            row = dict(components)
            for name in COMPONENTS:
                row[f"contrib_{name}"] = weights[name] * components[name]
            row["telemetry_fresh"] = 1.0 if worker.telemetry_fresh else 0.0
            row["telemetry_age_s"] = (
                -1.0 if worker.telemetry_age_s is None else worker.telemetry_age_s
            )
            row["telemetry_samples"] = float(worker.telemetry_samples)
            terms[worker.worker_id] = row

        selected = min(eligible, key=lambda w: (scores[w.worker_id], w.worker_id))
        stale = [w.worker_id for w in eligible if not w.telemetry_fresh]

        detail = (
            f"cpu {selected.cpu_percent:.1f}% (smoothed, {selected.telemetry_samples} samples)"
            if selected.telemetry_fresh
            else "telemetry stale -> reservation-derived estimate"
        )
        return PlacementOutcome(
            selected_worker=selected.worker_id,
            reason=(
                f"lowest adaptive score ({ALGORITHM_VERSION}, {job.workload_class.value}): "
                f"{selected.worker_id}={scores[selected.worker_id]:.4f} [{detail}]; "
                f"{len(eligible)} eligible, {len(excluded)} excluded"
                + (f", stale telemetry on {sorted(stale)}" if stale else "")
            ),
            scores=scores,
            terms=terms,
            excluded=excluded,
            weights=weights,
        )
