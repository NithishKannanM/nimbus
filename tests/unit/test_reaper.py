"""Reaper detection, in isolation.

The reaper decides *which* jobs are lost and *why*; the engine performs the
effects. That split is what lets these tests drive detection against a fake
reclaimer and assert on the classification, with no processes, no leases and no
real clock.

The integration side -- detect, requeue, complete elsewhere -- lives in
tests/failure/.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from backend.models.enums import JobEvent, JobStatus, WorkerStatus, WorkloadClass
from backend.models.job import Job, utcnow
from backend.services.reaper import Reaper, Reclamation
from backend.services.registry import WorkerRegistry
from backend.services.store.local import LocalStateStore


class FakeQueue:
    def __init__(self, expired: list[str] | None = None) -> None:
        self._expired = list(expired or [])
        self.sweeps = 0

    def sweep_expired(self) -> list[str]:
        self.sweeps += 1
        moved, self._expired = self._expired, []
        return moved

    def dead_letters(self) -> list[str]:
        return []


class FakeReclaimer:
    """Records claims and (by default) reports that it acted on them."""

    def __init__(self, *, succeed: bool = True) -> None:
        self.claims: list[Reclamation] = []
        self._succeed = succeed

    def __call__(self, claim: Reclamation) -> bool:
        self.claims.append(claim)
        return self._succeed

    def events(self) -> list[JobEvent]:
        return [c.event for c in self.claims]

    def job_ids(self) -> list[str]:
        return [c.job.job_id for c in self.claims]


def make_job(
    *,
    job_id: str = "job-1",
    status: JobStatus = JobStatus.RUNNING,
    worker: str | None = "worker-1",
    timeout_s: float = 10.0,
    started_ago_s: float = 1.0,
    attempts: int = 1,
) -> Job:
    now = utcnow()
    return Job(
        job_id=job_id,
        job_type="prime_count",
        workload_class=WorkloadClass.CPU_BOUND,
        status=status,
        assigned_worker=worker,
        attempts=attempts,
        timeout_s=timeout_s,
        created_at=now - timedelta(seconds=started_ago_s + 1),
        scheduled_at=now - timedelta(seconds=started_ago_s),
        started_at=now - timedelta(seconds=started_ago_s),
    )


@pytest.fixture
def store() -> LocalStateStore:
    return LocalStateStore()


@pytest.fixture
def registry() -> WorkerRegistry:
    reg = WorkerRegistry(heartbeat_timeout_s=15.0, eviction_timeout_s=60.0)
    for worker_id in ("worker-1", "worker-2"):
        reg.register(worker_id, slots=2, capacity_cpu_units=2048, capacity_memory_mb=4096)
    return reg


def build_reaper(store, registry, reclaimer, *, queue=None, leases=(), orphan_grace_s=30.0):
    held = set(leases)
    return Reaper(
        store=store,
        queue=queue or FakeQueue(),
        registry=registry,
        reclaim=reclaimer,
        has_lease=lambda job_id: job_id in held,
        default_timeout_s=300.0,
        orphan_grace_s=orphan_grace_s,
    )


# --------------------------------------------------------------------------- #
# timeouts
# --------------------------------------------------------------------------- #
def test_a_job_past_its_timeout_is_reaped(store, registry):
    store.put_job(make_job(timeout_s=5.0, started_ago_s=60.0))
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.timed_out == ["job-1"]
    assert reclaimer.events() == [JobEvent.TIMEOUT]
    assert "exceeded timeout_s=5" in reclaimer.claims[0].reason


def test_a_job_within_its_timeout_is_left_alone(store, registry):
    store.put_job(make_job(timeout_s=300.0, started_ago_s=5.0))
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.total == 0
    assert reclaimer.claims == []


def test_timeout_is_measured_from_acceptance_not_submission(store, registry):
    """Queue wait must not count against an execution timeout.

    If it did, the timeout would become a function of fleet load and a busy
    fleet would start killing perfectly healthy work -- turning a reliability
    mechanism into a load-dependent failure source, which would also corrupt
    every latency measurement the benchmark makes.
    """
    now = utcnow()
    job = make_job(timeout_s=10.0).model_copy(
        update={
            "created_at": now - timedelta(seconds=600),  # queued for ten minutes
            "scheduled_at": now - timedelta(seconds=2),
            "started_at": now - timedelta(seconds=2),  # running for two seconds
        }
    )
    store.put_job(job)
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.total == 0, "long queue wait was charged against the execution timeout"


def test_only_running_jobs_can_time_out(store, registry):
    """A SCHEDULED job has not started, so there is no execution to time out.
    It is the orphan and worker-loss paths that cover a job stuck there."""
    store.put_job(
        make_job(status=JobStatus.SCHEDULED, timeout_s=1.0, started_ago_s=100.0).model_copy(
            update={"started_at": None}
        )
    )
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.timed_out == []


# --------------------------------------------------------------------------- #
# lost workers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("age_s, expected", [(20.0, "UNHEALTHY"), (120.0, "DEAD")])
def test_jobs_on_a_silent_worker_are_reclaimed(store, registry, age_s, expected):
    store.put_job(make_job(worker="worker-1"))
    registry._workers["worker-1"].last_heartbeat -= age_s  # stop beating

    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.worker_lost == ["job-1"]
    assert reclaimer.events() == [JobEvent.WORKER_LOST]
    assert expected in reclaimer.claims[0].reason


def test_jobs_on_healthy_workers_are_not_reclaimed(store, registry):
    store.put_job(make_job(worker="worker-1"))
    reclaimer = FakeReclaimer()
    assert build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep().total == 0


def test_a_draining_worker_keeps_its_jobs(store, registry):
    """Draining is administrative, not a failure. Reclaiming in-flight jobs
    would turn a graceful drain into a forced one."""
    store.put_job(make_job(worker="worker-1"))
    registry.set_status("worker-1", WorkerStatus.DRAINING)

    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.total == 0
    assert reclaimer.claims == []


def test_worker_loss_outranks_timeout(store, registry):
    """A job that is both overdue and on a dead host is reported as
    worker-lost. That is the cause; the timeout is a consequence, and a trace
    saying 'timed out' would send a reader looking at the wrong thing."""
    store.put_job(make_job(timeout_s=1.0, started_ago_s=500.0, worker="worker-1"))
    registry._workers["worker-1"].last_heartbeat -= 300.0

    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.worker_lost == ["job-1"]
    assert report.timed_out == []


def test_queued_jobs_are_never_worker_lost(store, registry):
    """A QUEUED job is unassigned by definition; it belongs to the queue."""
    store.put_job(make_job(status=JobStatus.QUEUED, worker=None))
    registry._workers["worker-1"].last_heartbeat -= 300.0

    reclaimer = FakeReclaimer()
    assert build_reaper(store, registry, reclaimer).sweep().worker_lost == []


# --------------------------------------------------------------------------- #
# orphans
# --------------------------------------------------------------------------- #
def test_a_leaseless_job_past_the_grace_period_is_an_orphan(store, registry):
    store.put_job(make_job(started_ago_s=120.0, timeout_s=3600.0))
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=[], orphan_grace_s=30.0).sweep()

    assert report.orphaned == ["job-1"]
    assert "no lease" in reclaimer.claims[0].reason


def test_a_job_in_transit_is_not_an_orphan(store, registry):
    """The grace period exists so the reaper does not race the dispatch path.

    A job is briefly leaseless between the store write and the lease
    bookkeeping. Reclaiming it there would be the Phase 3 wait_for_idle bug
    arriving from the other direction -- a check that mistakes 'in transit' for
    'finished', except this one would re-place a running job.
    """
    store.put_job(make_job(started_ago_s=0.01, timeout_s=3600.0))
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=[], orphan_grace_s=30.0).sweep()

    assert report.orphaned == []


def test_a_held_lease_means_not_an_orphan(store, registry):
    store.put_job(make_job(started_ago_s=1000.0, timeout_s=3600.0))
    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert report.orphaned == []


# --------------------------------------------------------------------------- #
# dead letters
# --------------------------------------------------------------------------- #
def test_dead_lettered_messages_terminate_their_jobs(store, registry):
    """A dead-lettered message with a live job record is a job that can never
    resolve: the queue has stopped delivering it and nothing is running it."""
    store.put_job(make_job(job_id="job-poison", status=JobStatus.QUEUED, worker=None))
    queue = FakeQueue(expired=["job-poison"])

    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, queue=queue).sweep()

    assert report.dead_lettered == ["job-poison"]
    assert reclaimer.events() == [JobEvent.DEAD_LETTER]


def test_dead_lettering_a_finished_job_is_a_no_op(store, registry):
    store.put_job(make_job(job_id="job-done", status=JobStatus.COMPLETED, worker=None))
    queue = FakeQueue(expired=["job-done"])

    reclaimer = FakeReclaimer()
    report = build_reaper(store, registry, reclaimer, queue=queue).sweep()

    assert report.dead_lettered == []
    assert reclaimer.claims == []


def test_the_queue_is_swept_on_every_pass(store, registry):
    queue = FakeQueue()
    reaper = build_reaper(store, registry, FakeReclaimer(), queue=queue)
    reaper.sweep()
    reaper.sweep()
    assert queue.sweeps == 2


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def test_a_refused_reclaim_is_not_reported_as_reaped(store, registry):
    """The engine re-reads before acting and returns False when the job has
    already moved on. Counting that as a reclamation would inflate every
    reliability number the report quotes."""
    store.put_job(make_job(timeout_s=1.0, started_ago_s=100.0))
    reclaimer = FakeReclaimer(succeed=False)
    report = build_reaper(store, registry, reclaimer, leases=["job-1"]).sweep()

    assert reclaimer.claims, "the reaper should still have detected it"
    assert report.timed_out == []
    assert report.total == 0


def test_sweeps_are_counted_and_the_last_report_is_kept(store, registry):
    reaper = build_reaper(store, registry, FakeReclaimer())
    reaper.sweep()
    reaper.sweep()
    assert reaper.sweeps == 2
    assert reaper.last_report is not None
    assert reaper.last_report.as_dict()["total"] == 0


def test_report_serialises_every_category(store, registry):
    store.put_job(make_job(job_id="job-slow", timeout_s=1.0, started_ago_s=100.0))
    report = build_reaper(store, registry, FakeReclaimer(), leases=["job-slow"]).sweep()
    payload = report.as_dict()
    assert set(payload) == {
        "swept_at", "timed_out", "worker_lost", "orphaned", "dead_lettered", "total"
    }
    assert payload["timed_out"] == ["job-slow"]
    assert payload["total"] == 1
