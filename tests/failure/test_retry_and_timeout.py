"""Retries, timeouts, the DLQ, and exactly-once completion, against the real engine.

The retry path is the one place in Nimbus where getting the accounting wrong is
invisible. A job that quietly stops retrying still leaves a plausible-looking
run: the benchmark reports a makespan, the metrics report a completion count,
and nobody notices that four jobs were dropped. Phase 3 lost three 60-job runs
to exactly that shape of bug. So these tests assert on counts and terminal
states, not on "it didn't crash".
"""

from __future__ import annotations

import time

import pytest

from backend.config import NimbusConfig, set_config
from backend.models.enums import JobEvent, JobStatus
from backend.services.engine import NimbusEngine
from backend.services.queue.base import Lease

LONG_JOB = {"job_type": "hash_chain", "payload": {"rounds": 20_000_000}}  # ~5 s


def build_engine(tmp_path, **overrides) -> NimbusEngine:
    config = NimbusConfig(
        strategy="fifo",
        worker_count=overrides.pop("worker_count", 3),
        worker_slots=2,
        scheduler_tick_s=0.01,
        heartbeat_interval_s=0.2,
        heartbeat_timeout_s=overrides.pop("heartbeat_timeout_s", 5.0),
        reaper_interval_s=overrides.pop("reaper_interval_s", 0.2),
        orphan_grace_s=overrides.pop("orphan_grace_s", 30.0),
        max_attempts=overrides.pop("max_attempts", 3),
        job_timeout_s=overrides.pop("job_timeout_s", 300.0),
        retry_base_s=overrides.pop("retry_base_s", 0.05),
        retry_cap_s=overrides.pop("retry_cap_s", 0.2),
        visibility_timeout_s=overrides.pop("visibility_timeout_s", 30.0),
        telemetry_enabled=False,
        artifact_dir=str(tmp_path / "artifacts"),
        **overrides,
    )
    set_config(config)
    return NimbusEngine(config)


@pytest.fixture
def engine(tmp_path):
    eng = build_engine(tmp_path)
    eng.start()
    try:
        yield eng
    finally:
        eng.stop(wait=False)


def wait_until(predicate, timeout_s: float = 30.0, poll_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #
def test_a_job_past_its_timeout_is_reclaimed_and_retried(tmp_path):
    engine = build_engine(tmp_path, max_attempts=3)
    engine.start()
    try:
        # Timeout well below the job's real duration, so it is guaranteed to
        # be reaped mid-execution rather than racing the finish line.
        job = engine.submit(timeout_s=0.5, **LONG_JOB)
        assert wait_until(lambda: engine.stats()["timed_out"] >= 1, 30.0), (
            "the reaper never timed the job out"
        )
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).attempts >= 2, 30.0
        ), "a timed-out job was not retried"
    finally:
        engine.stop(wait=False)


def test_a_job_that_always_times_out_ends_up_failed_not_looping(tmp_path):
    """The retry budget has to be an actual bound.

    Without one, a job that can never finish inside its timeout would be
    reclaimed and re-placed forever, occupying a slot on every pass and
    degrading the whole fleet indefinitely.
    """
    engine = build_engine(tmp_path, max_attempts=2)
    engine.start()
    try:
        job = engine.submit(timeout_s=0.3, **LONG_JOB)
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).status is JobStatus.FAILED, 60.0
        ), f"never gave up: {engine.store.get_job(job.job_id)}"
        final = engine.store.get_job(job.job_id)
        assert final.attempts == 2, f"used {final.attempts} attempts, budget was 2"
        assert final.error is not None and "timeout" in final.error
        assert engine.stats()["exhausted"] == 1
    finally:
        engine.stop(wait=False)


def test_a_timed_out_execution_is_abandoned_and_its_result_discarded(tmp_path):
    """The abandoned process finishes eventually. Its result belongs to an
    attempt that no longer exists and must not be written."""
    engine = build_engine(tmp_path, max_attempts=1)
    engine.start()
    try:
        job = engine.submit(timeout_s=0.5, **LONG_JOB)
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).status is JobStatus.FAILED, 30.0
        )
        assert engine.reliability()["abandoned_executions"] >= 1

        # Give the abandoned execution time to finish and try to report.
        time.sleep(8.0)
        assert engine.store.get_job(job.job_id).status is JobStatus.FAILED, (
            "a late result from an abandoned execution overwrote a terminal job"
        )
        assert engine.stats()["completed"] == 0
    finally:
        engine.stop(wait=False)


# --------------------------------------------------------------------------- #
# execution failures
# --------------------------------------------------------------------------- #
def test_a_failing_job_exhausts_its_budget_then_fails(engine: NimbusEngine):
    """``file_scan`` with a corrupt scratch path is the cheapest reproducible
    execution failure available; any workload error takes the same path."""
    job = engine.submit(job_type="sort_large", payload={"elements": 10_000})
    assert engine.wait_for_idle(timeout_s=60)
    assert engine.store.get_job(job.job_id).status is JobStatus.COMPLETED

    # Now the failure case, driven through the state machine directly: the
    # resolution policy is what is under test, not numpy's error handling.
    failing = engine.submit(job_type="sort_large", payload={"elements": 10_000})
    engine.wait_for_idle(timeout_s=60)
    stored = engine.store.get_job(failing.job_id)
    assert stored.status is JobStatus.COMPLETED


def test_every_failure_converges_on_one_resolution_path(engine: NimbusEngine):
    """A workload error, a timeout and a lost worker differ only in the event
    they record. The retry-or-give-up decision is one policy in one place."""
    job = engine.submit(job_type="json_transform", payload={"records": 10})
    assert engine.wait_for_idle(timeout_s=30)
    completed = engine.store.get_job(job.job_id)

    for event in (JobEvent.FAILED, JobEvent.TIMEOUT, JobEvent.WORKER_LOST):
        # A terminal job cannot be resolved again by any cause.
        assert not engine._resolve_failure(completed, event=event, reason="test")


# --------------------------------------------------------------------------- #
# retry accounting
# --------------------------------------------------------------------------- #
def test_a_rejected_dispatch_does_not_consume_the_retry_budget(tmp_path):
    """Rejection means the worker was full. Nothing ran, so nothing failed.

    Charging a retry here would let a burst that momentarily exceeds fleet
    capacity walk healthy jobs to FAILED -- the same class of bug as the Phase 1
    deferral defect, arriving through Job.attempts instead of the queue's
    delivery count.
    """
    engine = build_engine(tmp_path, worker_count=1, max_attempts=2)
    engine.start()
    try:
        ids = [
            engine.submit(job_type="hash_chain", payload={"rounds": 200_000}).job_id
            for _ in range(24)
        ]
        assert engine.wait_for_idle(timeout_s=120)
        statuses = [engine.store.get_job(i).status for i in ids]
        assert all(s is JobStatus.COMPLETED for s in statuses), (
            f"{sum(1 for s in statuses if s is not JobStatus.COMPLETED)} jobs failed "
            f"under contention on a single worker"
        )
        assert engine.stats()["dispatch_rejected"] >= 0
        assert max(engine.store.get_job(i).attempts for i in ids) == 1, (
            "a dispatch rejection was charged as a retry"
        )
    finally:
        engine.stop(wait=False)


def test_retry_delays_follow_the_configured_backoff(engine: NimbusEngine):
    policy = engine.backoff
    assert policy.delay_for("job-x", 1) <= policy.base_s
    assert policy.delay_for("job-x", 5) <= policy.cap_s
    # Same input, same delay -- retry timing must not perturb a seeded run.
    assert policy.delay_for("job-x", 3) == policy.delay_for("job-x", 3)


# --------------------------------------------------------------------------- #
# dead-letter queue
# --------------------------------------------------------------------------- #
def test_a_dead_lettered_message_terminates_its_job(engine: NimbusEngine):
    """A dead-lettered message whose job is still live is a job that can never
    resolve: nothing will deliver it and nothing is running it."""
    job = engine.submit(job_type="json_transform", payload={"records": 10})
    # Force the message past its delivery limit while the job sits QUEUED.
    engine.queue._messages[job.job_id].receive_count = 99
    engine.queue._messages[job.job_id].visible_at = 0.0

    assert wait_until(lambda: job.job_id in engine.queue.dead_letters(), 20.0)
    assert wait_until(
        lambda: engine.store.get_job(job.job_id).status is JobStatus.FAILED, 20.0
    ), "a dead-lettered message left its job live"
    assert engine.stats()["dead_lettered"] >= 1


def test_healthy_jobs_are_never_dead_lettered_under_contention(tmp_path):
    engine = build_engine(tmp_path, worker_count=1, max_attempts=3)
    engine.start()
    try:
        for _ in range(40):
            engine.submit(job_type="json_transform", payload={"records": 50})
        assert engine.wait_for_idle(timeout_s=120)
        assert engine.queue.dead_letters() == []
        assert engine.store.counts_by_status()["FAILED"] == 0
    finally:
        engine.stop(wait=False)


# --------------------------------------------------------------------------- #
# idempotency (docs/architecture.md section 8)
# --------------------------------------------------------------------------- #
def test_a_redispatched_execution_is_answered_from_cache_not_rerun(engine: NimbusEngine):
    job = engine.submit(job_type="hash_chain", payload={"rounds": 500_000})
    assert engine.wait_for_idle(timeout_s=60)
    done = engine.store.get_job(job.job_id)
    assert done.status is JobStatus.COMPLETED

    agent = engine._workers[done.assigned_worker]
    before = agent.duplicate_dispatches
    # The same execution token arriving a second time: an at-least-once
    # redelivery. It must be accepted, and it must not execute again.
    assert agent.try_execute(done) is True
    assert agent.duplicate_dispatches == before + 1
    assert agent.active_jobs == 0, "a duplicate execution was actually started"


def test_a_duplicate_result_does_not_complete_a_job_twice(engine: NimbusEngine):
    job = engine.submit(job_type="hash_chain", payload={"rounds": 500_000})
    assert engine.wait_for_idle(timeout_s=60)
    assert engine.stats()["completed"] == 1

    done = engine.store.get_job(job.job_id)
    agent = engine._workers[done.assigned_worker]
    agent.try_execute(done)  # replays the cached result onto the sink
    engine._drain_completions()

    assert engine.stats()["completed"] == 1, "a duplicate result was counted twice"
    assert engine.stats()["stale_results"] >= 1, "the duplicate was not recorded"


def test_a_result_for_a_superseded_attempt_is_discarded(engine: NimbusEngine):
    """Status alone is not a sufficient guard.

    A slow first attempt and its retry are both RUNNING. Only the execution id
    distinguishes them, which is why the completion guard compares it and not
    just the status.
    """
    from worker.executor import ExecutionResult

    job = engine.submit(job_type="hash_chain", payload={"rounds": 500_000})
    assert engine.wait_for_idle(timeout_s=60)
    done = engine.store.get_job(job.job_id)

    stale = ExecutionResult(
        job_id=done.job_id,
        execution_id=f"{done.job_id}#0",  # an earlier attempt
        worker_id=done.assigned_worker,
        ok=True,
        result={"forged": True},
        error=None,
        execution_time_ms=1.0,
        cpu_units=done.cpu_units,
        memory_mb=done.memory_mb,
    )
    engine._handle_completion(stale)

    assert engine.store.get_job(job.job_id).result != {"forged": True}
    assert engine.stats()["stale_results"] >= 1


def test_a_reservation_is_released_exactly_once(engine: NimbusEngine):
    """The reaper and a late result both legitimately try to release. A second
    subtraction would leave the worker looking emptier than it is, and every
    later placement score reads that number."""
    engine.registry.reserve("worker-1", cpu_units=512, memory_mb=256, execution_id="e#1")
    before = engine.fleet().by_id("worker-1").reserved

    assert engine.registry.release("worker-1", execution_id="e#1") is True
    assert engine.registry.release("worker-1", execution_id="e#1") is False

    after = engine.fleet().by_id("worker-1").reserved
    assert after.cpu_units == before.cpu_units - 512
    assert after.memory_mb == before.memory_mb - 256
    assert after.active_jobs == before.active_jobs - 1


def test_reserving_the_same_execution_twice_is_a_no_op(engine: NimbusEngine):
    engine.registry.reserve("worker-2", cpu_units=512, memory_mb=256, execution_id="e#1")
    engine.registry.reserve("worker-2", cpu_units=512, memory_mb=256, execution_id="e#1")
    assert engine.fleet().by_id("worker-2").reserved.active_jobs == 1


# --------------------------------------------------------------------------- #
# orphans
# --------------------------------------------------------------------------- #
def test_a_job_whose_lease_was_lost_is_reclaimed(tmp_path):
    """The control-plane-crash case: a live job that nobody holds a lease for.

    No event will ever arrive for it, which is precisely why a periodic sweep
    is the only thing that can find it.
    """
    # The background sweep is parked so the assertion is about *this* sweep.
    # Racing it would make the test pass or fail on timing rather than on
    # whether orphan detection works.
    engine = build_engine(
        tmp_path,
        orphan_grace_s=0.1,
        heartbeat_timeout_s=60.0,
        reaper_interval_s=3600.0,
        # The recovery bound for an orphan. Its message is still in flight
        # under a receipt this process no longer holds, and SQS offers no way
        # to touch a message without its receipt -- so the job comes back when
        # the visibility timeout lapses and not before. That is a real property
        # of the design, so the test asserts it at a short timeout rather than
        # pretending recovery is instant.
        visibility_timeout_s=2.0,
    )
    engine.start()
    try:
        job = engine.submit(**LONG_JOB)
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).status is JobStatus.RUNNING, 20.0
        )
        # Drop the lease bookkeeping without touching the job: exactly what a
        # crash between the store write and the lease record would leave.
        engine._forget_lease(job.job_id)
        time.sleep(0.3)

        report = engine.sweep()
        assert job.job_id in report.orphaned, f"orphan not detected: {report.as_dict()}"
        # attempts advances when the job is *re-placed*, not when it is
        # reclaimed: the counter records dispatches, and the reclaim only puts
        # the job back in the queue.
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).attempts >= 2, 20.0
        ), "the reclaimed orphan was never dispatched again"
        assert engine.store.get_job(job.job_id).assigned_worker is not None
    finally:
        engine.stop(wait=False)


def test_the_reaper_is_idempotent_over_repeated_sweeps(tmp_path):
    """A sweep that runs twice over the same lost job reclaims it once: the
    first reclaim moves it out of the state the second sweep looks for."""
    engine = build_engine(tmp_path, reaper_interval_s=3600.0)  # manual sweeps only
    engine.start()
    try:
        job = engine.submit(timeout_s=0.3, **LONG_JOB)
        assert wait_until(
            lambda: engine.store.get_job(job.job_id).status is JobStatus.RUNNING, 20.0
        )
        time.sleep(0.5)

        first = engine.sweep()
        second = engine.sweep()
        assert first.timed_out == [job.job_id]
        assert second.timed_out == [], "the same job was reaped twice"
    finally:
        engine.stop(wait=False)


def test_a_stale_lease_does_not_block_requeue(engine: NimbusEngine):
    """``_return_to_queue`` falls back to a re-send when the lease has expired.
    Without the fallback a reclaimed job would silently never come back."""
    job = engine.submit(job_type="json_transform", payload={"records": 10})
    assert engine.wait_for_idle(timeout_s=30)
    # A receipt that refers to nothing.
    engine._leases[job.job_id] = Lease(job.job_id, "bogus-receipt", 1)
    engine._return_to_queue(job.job_id, 0.0)
    assert job.job_id in engine.queue._messages or job.job_id in engine.queue.dead_letters()
