"""Metric definitions, especially load balance.

Phase 1 produced a run with job counts 16/7/4 and busy times 2.10/2.24/2.22 s.
Job-count balance and utilisation balance disagreed sharply, and utilisation is
the one that determines makespan. These tests pin that distinction.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.models.enums import JobStatus
from backend.models.job import Job, utcnow
from benchmark.metrics import LatencyStats, summarise

SLOTS = {"worker-1": 2, "worker-2": 2, "worker-3": 2}


def completed_job(worker: str, *, created_offset: float, start_offset: float, exec_ms: float) -> Job:
    base = utcnow()
    started = base + timedelta(seconds=start_offset)
    return Job(
        job_type="prime_count",
        workload_class="CPU_BOUND",
        status=JobStatus.COMPLETED,
        assigned_worker=worker,
        attempts=1,
        created_at=base + timedelta(seconds=created_offset),
        scheduled_at=started,
        started_at=started,
        completed_at=started + timedelta(milliseconds=exec_ms),
        execution_time_ms=exec_ms,
    )


def test_latency_stats_of_empty_is_zeroed():
    stats = LatencyStats.of([])
    assert stats.n == 0 and stats.p95 == 0.0


def test_percentiles_use_nearest_rank():
    stats = LatencyStats.of([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    assert stats.p50 == 5.5
    assert stats.p95 == 10
    assert stats.max == 10


def test_uneven_job_counts_can_still_be_balanced_utilisation():
    """The Phase 1 observation, encoded: 16/7/4 jobs but near-equal busy time."""
    jobs = (
        [completed_job("worker-1", created_offset=0, start_offset=0, exec_ms=131.0) for _ in range(16)]
        + [completed_job("worker-2", created_offset=0, start_offset=0, exec_ms=320.0) for _ in range(7)]
        + [completed_job("worker-3", created_offset=0, start_offset=0, exec_ms=555.0) for _ in range(4)]
    )
    metrics = summarise(jobs, [], slots_by_worker=SLOTS)

    balance = metrics.load_balance
    assert balance.job_count_by_worker == {"worker-1": 16, "worker-2": 7, "worker-3": 4}
    assert balance.job_count_cv > 0.5, "job counts look badly imbalanced"
    assert balance.utilisation_cv < 0.05, "utilisation is nearly perfectly balanced"
    assert balance.jain_fairness > 0.99


def test_perfect_balance_has_zero_cv_and_unit_fairness():
    jobs = [
        completed_job(w, created_offset=0, start_offset=0, exec_ms=100.0)
        for w in SLOTS for _ in range(3)
    ]
    balance = summarise(jobs, [], slots_by_worker=SLOTS).load_balance
    assert balance.utilisation_cv == pytest.approx(0.0)
    assert balance.jain_fairness == pytest.approx(1.0)


def test_single_worker_hoarding_shows_high_cv():
    jobs = [
        completed_job("worker-1", created_offset=0, start_offset=0, exec_ms=500.0)
        for _ in range(9)
    ]
    balance = summarise(jobs, [], slots_by_worker=SLOTS).load_balance
    assert balance.utilisation_cv > 1.0
    assert balance.jain_fairness < 0.4


def test_completion_queue_and_execution_are_distinguished():
    job = completed_job("worker-1", created_offset=0, start_offset=2.0, exec_ms=500.0)
    metrics = summarise([job], [], slots_by_worker=SLOTS)
    assert metrics.queue_wait_ms.p50 == pytest.approx(2000.0, abs=1.0)
    assert metrics.execution_ms.p50 == pytest.approx(500.0)
    assert metrics.completion_ms.p50 == pytest.approx(2500.0, abs=1.0)


def test_makespan_and_throughput():
    jobs = [
        completed_job("worker-1", created_offset=0, start_offset=0, exec_ms=1000.0),
        completed_job("worker-2", created_offset=0, start_offset=1.0, exec_ms=1000.0),
    ]
    metrics = summarise(jobs, [], slots_by_worker=SLOTS)
    assert metrics.makespan_s == pytest.approx(2.0, abs=0.05)
    assert metrics.throughput_jobs_s == pytest.approx(1.0, abs=0.05)


def test_failed_and_unfinished_jobs_are_counted_separately():
    jobs = [
        completed_job("worker-1", created_offset=0, start_offset=0, exec_ms=100.0),
        Job(job_type="prime_count", workload_class="CPU_BOUND", status=JobStatus.FAILED, attempts=3),
        Job(job_type="prime_count", workload_class="CPU_BOUND", status=JobStatus.QUEUED),
    ]
    metrics = summarise(jobs, [], slots_by_worker=SLOTS)
    assert (metrics.jobs_completed, metrics.jobs_failed, metrics.jobs_unfinished) == (1, 1, 1)
    assert metrics.failure_rate == pytest.approx(1 / 3)
    assert metrics.retry_rate == pytest.approx(2 / 3)


def test_no_completed_jobs_does_not_divide_by_zero():
    metrics = summarise(
        [Job(job_type="prime_count", workload_class="CPU_BOUND")], [], slots_by_worker=SLOTS
    )
    assert metrics.makespan_s == 0.0
    assert metrics.throughput_jobs_s == 0.0


# --------------------------------------------------------------------------- #
# oscillation instrumentation (adaptive-v1 pre-registration section 5)
# --------------------------------------------------------------------------- #
def decision_at(worker: str, offset_s: float):
    from backend.models.decision import SchedulingDecision

    return SchedulingDecision(
        job_id=f"job-{offset_s}",
        attempt=1,
        strategy="adaptive",
        ordering_policy="fifo",
        placement_policy="adaptive",
        workload_class="CPU_BOUND",
        priority=3,
        effective_priority=3,
        decided_at=utcnow() + timedelta(seconds=offset_s),
        selected_worker=worker,
    )


def test_switch_rate_is_zero_when_every_job_goes_to_one_worker():
    from benchmark.metrics import placement_dynamics

    decisions = [decision_at("worker-1", i) for i in range(6)]
    assert placement_dynamics(decisions, window_s=2.0).switch_rate == 0.0


def test_switch_rate_is_one_for_perfect_rotation():
    from benchmark.metrics import placement_dynamics

    workers = ["worker-1", "worker-2", "worker-3"] * 3
    decisions = [decision_at(w, i) for i, w in enumerate(workers)]
    assert placement_dynamics(decisions, window_s=2.0).switch_rate == 1.0


def test_concentration_detects_herding_within_one_telemetry_window():
    """Six jobs dispatched inside one sampling interval all saw the same stale
    snapshot; if they all land on one worker, that is telemetry-lag herding."""
    from benchmark.metrics import placement_dynamics

    herded = [decision_at("worker-1", 0.1 * i) for i in range(6)]
    assert placement_dynamics(herded, window_s=2.0).concentration_max == 1.0

    spread = [
        decision_at(w, 0.1 * i)
        for i, w in enumerate(["worker-1", "worker-2", "worker-3"] * 2)
    ]
    assert placement_dynamics(spread, window_s=2.0).concentration_max == pytest.approx(1 / 3)


def test_dynamics_are_empty_for_a_trivial_run():
    from benchmark.metrics import placement_dynamics

    assert placement_dynamics([], window_s=2.0).windows == 0
    assert placement_dynamics([decision_at("worker-1", 0)], window_s=2.0).switch_rate == 0.0


def test_load_regime_gate_matches_the_preregistered_threshold():
    """Runs below 0.50 mean fleet utilisation are descriptive only -- the R9
    guard against Static's low-load tie-break degeneracy."""
    # Two sequential jobs per worker, 1 slot of 2 busy throughout => util 0.5.
    busy = [
        completed_job(w, created_offset=0, start_offset=1.8 * i, exec_ms=1800.0)
        for w in SLOTS for i in range(2)
    ]
    metrics = summarise(busy, [], slots_by_worker=SLOTS)
    assert metrics.mean_fleet_utilisation == pytest.approx(0.5, abs=0.02)

    # Both slots busy throughout => comfortably inside the primary regime.
    saturated = [
        completed_job(w, created_offset=0, start_offset=1.8 * i, exec_ms=1800.0)
        for w in SLOTS for i in range(2) for _ in range(2)
    ]
    assert summarise(saturated, [], slots_by_worker=SLOTS).load_regime_ok is True

    idle = [completed_job("worker-1", created_offset=0, start_offset=0, exec_ms=10.0)]
    idle.append(completed_job("worker-2", created_offset=0, start_offset=9.0, exec_ms=10.0))
    assert summarise(idle, [], slots_by_worker=SLOTS).load_regime_ok is False
