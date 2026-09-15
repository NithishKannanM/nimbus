"""Phase 1 exit criterion: submit -> queue -> scheduler -> worker -> result.

These run the real engine with real process-pool workers executing real
computations. Nothing is mocked.
"""

from __future__ import annotations

import pytest

from backend.models.enums import JobStatus
from backend.services.engine import NimbusEngine
from tests.conftest import SMALL_PAYLOADS
from worker.workloads.registry import JOB_TYPES


def test_single_job_completes_with_a_real_result(engine: NimbusEngine):
    job = engine.submit(job_type="prime_count", payload={"limit": 100_000})
    assert job.status is JobStatus.QUEUED

    assert engine.wait_for_idle(timeout_s=60), "pipeline did not drain"

    done = engine.store.get_job(job.job_id)
    assert done.status is JobStatus.COMPLETED
    assert done.assigned_worker in {"worker-1", "worker-2", "worker-3"}
    assert done.result["primes"] == 9592  # primes below 100_000
    assert done.execution_time_ms > 0
    assert done.attempts == 1
    assert done.started_at >= done.scheduled_at >= done.created_at


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_every_workload_class_runs_end_to_end(engine: NimbusEngine, job_type: str):
    job = engine.submit(job_type=job_type, payload=SMALL_PAYLOADS[job_type])
    assert engine.wait_for_idle(timeout_s=90)
    done = engine.store.get_job(job.job_id)
    assert done.status is JobStatus.COMPLETED, done.error
    assert "checksum" in done.result


def test_heterogeneous_batch_drains_and_spreads_across_the_fleet(engine: NimbusEngine):
    jobs = [
        engine.submit(job_type=job_type, payload=SMALL_PAYLOADS[job_type])
        for job_type in JOB_TYPES
        for _ in range(2)
    ]
    assert engine.wait_for_idle(timeout_s=120)

    finished = [engine.store.get_job(j.job_id) for j in jobs]
    assert all(f.status is JobStatus.COMPLETED for f in finished), [
        (f.job_id, f.status, f.error) for f in finished if f.status is not JobStatus.COMPLETED
    ]

    used = {f.assigned_worker for f in finished}
    assert len(used) == 3, f"FIFO round-robin should use all three workers, used {used}"

    counts = engine.store.counts_by_status()
    assert counts["COMPLETED"] == len(jobs)
    assert counts["FAILED"] == 0


def test_queue_is_empty_and_nothing_dead_lettered(engine: NimbusEngine):
    for _ in range(6):
        engine.submit(job_type="json_transform", payload={"records": 200})
    assert engine.wait_for_idle(timeout_s=60)
    assert engine.queue_depth().total == 0
    assert engine.queue.dead_letters() == []


def test_every_placement_produces_an_explainable_decision(engine: NimbusEngine):
    job = engine.submit(job_type="hash_chain", payload={"rounds": 20_000})
    assert engine.wait_for_idle(timeout_s=60)

    decisions = engine.decisions(job_id=job.job_id)
    assert len(decisions) >= 1
    decision = decisions[0]
    assert decision.strategy == "fifo"
    assert decision.selected_worker == engine.store.get_job(job.job_id).assigned_worker
    assert decision.candidates, "the trace must list the candidates considered"
    assert decision.reason
    assert decision.decision_latency_us > 0
    assert decision.fleet_snapshot_at is not None


def test_slot_limit_is_respected(engine: NimbusEngine):
    """3 workers x 2 slots = 6 concurrent jobs, never more."""
    for _ in range(12):
        engine.submit(job_type="matrix_multiply", payload={"size": 220})

    observed_max = 0
    for _ in range(400):
        observed_max = max(
            observed_max, sum(w.reserved.active_jobs for w in engine.fleet().workers)
        )
        if engine.queue_depth().total == 0:
            break
        import time
        time.sleep(0.01)

    assert engine.wait_for_idle(timeout_s=120)
    assert observed_max <= 6, f"oversubscribed the fleet: {observed_max} concurrent"
    assert observed_max >= 2, (
        "never observed real concurrency -- this test would pass vacuously "
        f"on a serialised fleet (max seen: {observed_max})"
    )
    assert all(w.reserved.active_jobs == 0 for w in engine.fleet().workers)
    assert all(w.reserved.cpu_units == 0 for w in engine.fleet().workers)


def test_failing_workload_is_recorded_not_swallowed(engine: NimbusEngine):
    """A workload that raises inside the pool must surface as FAILED with the
    error text, not vanish."""
    job = engine.submit(job_type="prime_count", payload={"limit": 10_000})
    # Corrupt the stored payload so the pool child raises on validation.
    stored = engine.store.get_job(job.job_id)
    engine.store._jobs[job.job_id] = stored.model_copy(  # noqa: SLF001 -- test seam
        update={"payload": {"limit": -5}}
    )
    assert engine.wait_for_idle(timeout_s=60)

    done = engine.store.get_job(job.job_id)
    assert done.status is JobStatus.FAILED
    assert done.error and "ValidationError" in done.error


def test_resource_contention_never_dead_letters_healthy_jobs(engine: NimbusEngine):
    """Regression: a job that momentarily has no eligible worker must be
    deferred, not counted as a delivery.

    Deferral used to release the lease, which kept ApproximateReceiveCount
    climbing; a burst of CPU_BOUND jobs that could not all be placed at once
    walked healthy jobs into the DLQ without a single failure.
    """
    jobs = [
        engine.submit(job_type="matrix_multiply", payload={"size": 260})
        for _ in range(30)
    ]
    assert engine.wait_for_idle(timeout_s=180)

    assert engine.queue.dead_letters() == [], "healthy jobs must never be dead-lettered"
    finished = [engine.store.get_job(j.job_id) for j in jobs]
    stranded = [f for f in finished if f.status is not JobStatus.COMPLETED]
    assert not stranded, [(f.job_id, f.status.value, f.error) for f in stranded]
    assert all(f.attempts == 1 for f in finished), "deferral must not consume attempts"


def test_declared_capacity_allows_a_full_slot_of_cpu_bound_work(engine: NimbusEngine):
    """A 2-slot worker must be able to hold two CPU_BOUND jobs at once,
    otherwise half the fleet's declared slots are unreachable."""
    for _ in range(12):
        engine.submit(job_type="prime_count", payload={"limit": 2_000_000})

    peak = 0
    import time
    for _ in range(600):
        peak = max(peak, sum(w.reserved.active_jobs for w in engine.fleet().workers))
        if engine.queue_depth().total == 0:
            break
        time.sleep(0.01)
    assert engine.wait_for_idle(timeout_s=180)
    assert peak >= 4, f"CPU-bound jobs could not fill the fleet (peak {peak}/6)"


def test_wait_for_idle_does_not_return_while_jobs_are_unfinished(engine: NimbusEngine):
    """Regression: the drain check looked only at queue depth and worker
    busyness, so a job in transit between control-plane steps could be counted
    as 'finished'. It must consult job state too."""
    jobs = [
        engine.submit(job_type="matrix_multiply", payload={"size": 300})
        for _ in range(24)
    ]
    assert engine.wait_for_idle(timeout_s=240)

    finished = [engine.store.get_job(j.job_id) for j in jobs]
    unfinished = [f for f in finished if f.status is not JobStatus.COMPLETED]
    assert not unfinished, [(f.job_id, f.status.value) for f in unfinished]

    counts = engine.store.counts_by_status()
    for state in ("QUEUED", "SCHEDULED", "RUNNING", "RETRYING", "TIMED_OUT"):
        assert counts[state] == 0, f"{state} jobs remained after wait_for_idle"
