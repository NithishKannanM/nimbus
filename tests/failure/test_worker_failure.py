"""The Phase 4 exit criterion, end to end: **detect, requeue, complete elsewhere.**

Nothing here is mocked. A worker's executor is genuinely destroyed, its child
processes are killed, and its heartbeat stops. The control plane is not told;
it has to infer the loss from silence, reclaim the stranded jobs, and re-place
them on a worker that is still alive.

That distinction is the whole point. If the engine simply set a worker's status
to UNHEALTHY, these tests would exercise a setter. Detection is the thing that
has to work, because on EC2 nothing will call an API to announce that an
instance has gone away.
"""

from __future__ import annotations

import time

import pytest

from backend.config import NimbusConfig, set_config
from backend.models.enums import JobStatus, WorkerStatus
from backend.services.engine import NimbusEngine


def build_engine(tmp_path, **overrides) -> NimbusEngine:
    config = NimbusConfig(
        strategy=overrides.pop("strategy", "fifo"),
        worker_count=overrides.pop("worker_count", 3),
        worker_slots=overrides.pop("worker_slots", 2),
        scheduler_tick_s=0.01,
        # Compressed so a failure is detected in a test rather than in a minute.
        # The mechanism is the same at production values; only the constants
        # differ, and they are configuration rather than code.
        heartbeat_interval_s=overrides.pop("heartbeat_interval_s", 0.2),
        heartbeat_timeout_s=overrides.pop("heartbeat_timeout_s", 1.0),
        eviction_timeout_s=overrides.pop("eviction_timeout_s", 30.0),
        reaper_interval_s=overrides.pop("reaper_interval_s", 0.2),
        orphan_grace_s=overrides.pop("orphan_grace_s", 30.0),
        retry_base_s=overrides.pop("retry_base_s", 0.05),
        retry_cap_s=overrides.pop("retry_cap_s", 0.5),
        telemetry_enabled=False,  # irrelevant here, and it costs a thread
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


#: A job long enough that failure detection reliably happens *while it runs*.
#: This matters more than it looks: most Nimbus workloads are numpy-backed and
#: finish in tens of milliseconds, and a job that completes before the failure
#: is noticed makes these tests pass for the wrong reason. hash_chain is the
#: one CPU_BOUND workload that is a real interpreter loop rather than a vector
#: operation, so its duration scales predictably with its parameter.
LONG_JOB = {"job_type": "hash_chain", "payload": {"rounds": 20_000_000}}  # ~5 s
SHORT_JOB = {"job_type": "hash_chain", "payload": {"rounds": 1_000_000}}  # ~0.25 s


def wait_until(predicate, timeout_s: float = 30.0, poll_s: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def wait_for_status(engine: NimbusEngine, job_id: str, status: JobStatus, timeout_s=30.0) -> bool:
    return wait_until(lambda: engine.store.get_job(job_id).status is status, timeout_s)


def worker_of(engine: NimbusEngine, job_id: str) -> str | None:
    return engine.store.get_job(job_id).assigned_worker


# --------------------------------------------------------------------------- #
# the exit criterion
# --------------------------------------------------------------------------- #
def test_killing_a_worker_requeues_its_job_and_it_completes_elsewhere(engine: NimbusEngine):
    """Kill the worker running a job; the job must finish on a different one."""
    job = engine.submit(**LONG_JOB)
    assert wait_until(lambda: worker_of(engine, job.job_id) is not None, 10.0)
    victim = worker_of(engine, job.job_id)

    engine.fail_worker(victim, hard=True)

    assert wait_for_status(engine, job.job_id, JobStatus.COMPLETED, timeout_s=60.0), (
        f"job did not recover; final state {engine.store.get_job(job.job_id)}"
    )
    final = engine.store.get_job(job.job_id)
    assert final.assigned_worker != victim, "job completed on the worker that was killed"
    assert final.attempts >= 2, "recovery should have consumed a second attempt"
    assert final.result is not None


def test_the_loss_is_detected_not_announced(engine: NimbusEngine):
    """The engine never marks the worker unhealthy. It stops hearing from it.

    This is the property that has to hold for the mechanism to transfer to
    EC2: an instance that goes away sends no notification.
    """
    engine.submit(**LONG_JOB)
    assert wait_until(lambda: any(a.active_jobs for a in engine._workers.values()), 10.0)
    victim = next(w for w, a in engine._workers.items() if a.active_jobs)

    assert engine.fleet().by_id(victim).status is WorkerStatus.HEALTHY
    engine.fail_worker(victim, hard=True)

    # Still HEALTHY immediately after: nothing has been told anything yet.
    assert engine.fleet().by_id(victim).status is WorkerStatus.HEALTHY

    assert wait_until(
        lambda: engine.fleet().by_id(victim).status is WorkerStatus.UNHEALTHY, 10.0
    ), "heartbeat expiry never moved the worker to UNHEALTHY"


def test_a_dead_workers_reservations_are_released(engine: NimbusEngine):
    """A dead worker that still looks busy poisons every later placement score,
    because reservations are an input to all three static policies."""
    for _ in range(2):
        engine.submit(**LONG_JOB)
    assert wait_until(lambda: any(a.active_jobs for a in engine._workers.values()), 10.0)
    victim = next(w for w, a in engine._workers.items() if a.active_jobs)

    engine.fail_worker(victim, hard=True)
    assert wait_until(
        lambda: engine.fleet().by_id(victim).reserved.active_jobs == 0, 20.0
    ), "reservations survived the worker they were held on"
    assert engine.registry.reserved_executions(victim) == ()


def test_a_recovered_worker_is_used_again(engine: NimbusEngine):
    victim = "worker-1"
    engine.fail_worker(victim, hard=True)
    assert wait_until(lambda: engine.fleet().by_id(victim).status is WorkerStatus.UNHEALTHY, 10.0)

    engine.recover_worker(victim)
    assert engine.fleet().by_id(victim).status is WorkerStatus.HEALTHY

    ids = [engine.submit(job_type="json_transform", payload={"records": 200}).job_id
           for _ in range(30)]
    assert engine.wait_for_idle(timeout_s=60)
    used = {engine.store.get_job(i).assigned_worker for i in ids}
    assert victim in used, "recovered worker never received work again"


def test_losing_a_worker_does_not_lose_a_batch(engine: NimbusEngine):
    """The real requirement. Kill a worker mid-batch; every job still finishes.

    Job loss here would be invisible in aggregate metrics -- a run that
    completes 38 of 40 jobs still reports a makespan -- which is exactly how
    the Phase 3 deferral bug hid for three benchmark runs.
    """
    ids = [engine.submit(**SHORT_JOB).job_id for _ in range(40)]
    assert wait_until(lambda: any(a.active_jobs for a in engine._workers.values()), 10.0)
    victim = next(w for w, a in engine._workers.items() if a.active_jobs)
    engine.fail_worker(victim, hard=True)

    assert engine.wait_for_idle(timeout_s=180), "batch never drained after a worker died"
    statuses = [engine.store.get_job(i).status for i in ids]
    assert all(s is JobStatus.COMPLETED for s in statuses), (
        f"{sum(1 for s in statuses if s is not JobStatus.COMPLETED)} of {len(ids)} did not complete"
    )
    assert engine.queue.dead_letters() == []


# --------------------------------------------------------------------------- #
# partition: the duplicate-execution case
# --------------------------------------------------------------------------- #
def test_a_partitioned_worker_keeps_running_and_its_result_is_discarded(engine: NimbusEngine):
    """The honest version of at-least-once.

    A soft failure leaves the process running: it will finish and report. By
    then the control plane has reclaimed the job and re-placed it, so the late
    result belongs to a superseded attempt and must be discarded rather than
    written. That guard is what makes the job *complete* once even though it
    may *execute* twice (docs/architecture.md section 8).

    The job has to outlast detection for the race to happen at all -- a short
    job simply finishes before the partition is noticed, which is correct
    behaviour but tests nothing.
    """
    job = engine.submit(**LONG_JOB)
    assert wait_until(lambda: worker_of(engine, job.job_id) is not None, 10.0)
    victim = worker_of(engine, job.job_id)

    engine.fail_worker(victim, hard=False)  # partition, not a crash

    assert wait_for_status(engine, job.job_id, JobStatus.COMPLETED, timeout_s=90.0)
    final = engine.store.get_job(job.job_id)
    assert final.assigned_worker != victim
    # Exactly one completion, whatever happened underneath.
    assert engine.stats()["completed"] == 1
    assert engine.reliability()["abandoned_executions"] >= 1


# --------------------------------------------------------------------------- #
# draining
# --------------------------------------------------------------------------- #
def test_draining_stops_new_placement_without_killing_running_work(engine: NimbusEngine):
    job = engine.submit(**LONG_JOB)
    assert wait_until(lambda: worker_of(engine, job.job_id) is not None, 10.0)
    target = worker_of(engine, job.job_id)

    engine.drain_worker(target)
    assert engine.fleet().by_id(target).status is WorkerStatus.DRAINING

    ids = [engine.submit(job_type="json_transform", payload={"records": 100}).job_id
           for _ in range(30)]
    assert engine.wait_for_idle(timeout_s=90)

    assert engine.store.get_job(job.job_id).status is JobStatus.COMPLETED, (
        "draining reclaimed a job that should have been allowed to finish"
    )
    placed_on_drained = [i for i in ids if engine.store.get_job(i).assigned_worker == target]
    assert placed_on_drained == [], "a draining worker received new work"
