"""Live, per-strategy comparison for the dashboard.

Groups this control plane's own history by the strategy that placed each job,
so an operator who switches strategy mid-stream can watch the two compared
side by side.

This is NOT the controlled comparison. Strategies here ran one after another,
under whatever load happened to be present at the time, on different jobs. The
benchmark runner (same seeded workload, same pressure, repeated runs) is the
only evidence that supports a claim; this is the demonstration of the mechanism.

Attribution rule: a job belongs to the strategy of its *last* placement
decision, which is the one that dispatched the attempt that finished. A job
retried across a switch therefore counts once, for the strategy that placed
its final attempt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

from backend.models.decision import SchedulingDecision
from backend.models.enums import JobStatus
from backend.models.job import Job

#: Observed CPU at or above this, on the selected worker at decision time,
#: counts as a placement onto a saturated host.
HOT_CPU_PERCENT = 80.0


@dataclass
class StrategySegment:
    """One continuous stretch during which a strategy was active."""

    strategy: str
    placement_policy: str
    started_at: datetime
    ended_at: datetime | None = None

    def seconds(self, now: datetime) -> float:
        return max(0.0, ((self.ended_at or now) - self.started_at).total_seconds())


def _percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return round(ordered[rank - 1], 3)


def compare_strategies(
    decisions: Iterable[SchedulingDecision],
    jobs: Iterable[Job],
    segments: list[StrategySegment],
    now: datetime,
    *,
    hot_cpu_percent: float = HOT_CPU_PERCENT,
) -> dict[str, Any]:
    # Latest placing decision per job. Decisions arrive newest first, so the
    # first one seen for a job is its final placement.
    placing: dict[str, SchedulingDecision] = {}
    placed: dict[str, int] = {}
    placed_hot: dict[str, int] = {}
    for decision in decisions:
        if decision.selected_worker is None:
            continue
        placing.setdefault(decision.job_id, decision)
        placed[decision.strategy] = placed.get(decision.strategy, 0) + 1
        cpu = decision.observed_cpu(decision.selected_worker)
        if cpu is not None and cpu >= hot_cpu_percent:
            placed_hot[decision.strategy] = placed_hot.get(decision.strategy, 0) + 1

    exec_s: dict[str, list[float]] = {}
    for job in jobs:
        if job.status is not JobStatus.COMPLETED or job.execution_time_ms is None:
            continue
        decision = placing.get(job.job_id)
        if decision is None:
            continue  # placed before the retained decision history began
        exec_s.setdefault(decision.strategy, []).append(job.execution_time_ms / 1000.0)

    # One row per strategy, in the order each was first switched on.
    order: list[str] = []
    active_s: dict[str, float] = {}
    policy: dict[str, str] = {}
    for segment in segments:
        if segment.strategy not in active_s:
            order.append(segment.strategy)
            active_s[segment.strategy] = 0.0
        active_s[segment.strategy] += segment.seconds(now)
        policy[segment.strategy] = segment.placement_policy

    rows = []
    for name in order:
        times = exec_s.get(name, [])
        minutes = active_s[name] / 60.0
        rows.append(
            {
                "strategy": name,
                "placement_policy": policy[name],
                "reads_telemetry": policy[name].startswith("adaptive"),
                "active_s": round(active_s[name], 1),
                "placed": placed.get(name, 0),
                "placed_hot": placed_hot.get(name, 0),
                "completed": len(times),
                "completed_per_min": round(len(times) / minutes, 2) if minutes > 0 else None,
                "mean_exec_s": round(sum(times) / len(times), 3) if times else None,
                "p95_exec_s": _percentile(times, 0.95),
            }
        )

    return {
        "since": segments[0].started_at if segments else now,
        "hot_cpu_percent": hot_cpu_percent,
        "active": segments[-1].strategy if segments else None,
        "strategies": rows,
    }
