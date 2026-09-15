"""The reaper: failure detection for work nobody will ever report on.

Every other failure path in Nimbus is *reported*. A workload raises and the
executor returns an error envelope; a worker is at capacity and rejects a
dispatch. Those arrive as events, and the engine handles them where they land.

The reaper exists for the failures that produce no event at all:

- a worker process dies mid-job, so no result is ever pushed;
- a job runs past its wall-clock timeout and would otherwise run forever;
- a job is in a non-terminal state in the store but nothing holds its lease,
  which means the control plane lost track of it (a crash between two steps).

Nothing detects any of these except a periodic sweep, so a periodic sweep is
what this is. It is deliberately **detection only**: the reaper decides *which
jobs are lost and why*, and hands each one to a ``reclaim`` callback supplied by
the engine, which owns the leases, the reservations and the worker agents. That
split keeps the detection logic unit-testable against a fake reclaimer, and
keeps the effects in the one object that can actually perform them.

Detection is idempotent. A sweep that runs twice over the same lost job reclaims
it once, because the first reclaim moves it out of the state the second sweep
looks for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Protocol

from backend.models.enums import JobEvent, JobStatus, WorkerStatus
from backend.models.job import Job, utcnow

log = logging.getLogger("nimbus.reaper")

#: Job statuses in which a job is somebody's responsibility: it is either
#: waiting to be placed, placed, or running. Anything here that no longer has
#: an owner is lost.
LIVE_STATUSES = (
    JobStatus.QUEUED,
    JobStatus.SCHEDULED,
    JobStatus.RUNNING,
    JobStatus.RETRYING,
    JobStatus.TIMED_OUT,
)

#: Statuses a worker's failure can strand. QUEUED jobs are unassigned by
#: definition and are already the queue's problem.
ASSIGNED_STATUSES = (JobStatus.SCHEDULED, JobStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class Reclamation:
    """One lost job, and the transition the engine should apply to it."""

    job: Job
    event: JobEvent
    reason: str
    worker_id: str | None = None


@dataclass
class ReaperReport:
    """What one sweep found. Returned rather than logged-and-forgotten so the
    failure tests can assert on it and the API can report it."""

    swept_at: datetime = field(default_factory=utcnow)
    timed_out: list[str] = field(default_factory=list)
    worker_lost: list[str] = field(default_factory=list)
    orphaned: list[str] = field(default_factory=list)
    dead_lettered: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return (
            len(self.timed_out)
            + len(self.worker_lost)
            + len(self.orphaned)
            + len(self.dead_lettered)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "swept_at": self.swept_at.isoformat(),
            "timed_out": list(self.timed_out),
            "worker_lost": list(self.worker_lost),
            "orphaned": list(self.orphaned),
            "dead_lettered": list(self.dead_lettered),
            "total": self.total,
        }


class ReaperQueue(Protocol):
    """The slice of ``JobQueue`` the reaper needs."""

    def sweep_expired(self) -> list[str]: ...
    def dead_letters(self) -> list[str]: ...


#: Supplied by the engine. Performs the effects the reaper only decides on.
ReclaimFn = Callable[[Reclamation], bool]
#: Returns True when the control plane still holds a lease for this job.
HasLeaseFn = Callable[[str], bool]


class Reaper:
    def __init__(
        self,
        *,
        store,
        queue: ReaperQueue,
        registry,
        reclaim: ReclaimFn,
        has_lease: HasLeaseFn,
        default_timeout_s: float = 300.0,
        orphan_grace_s: float = 30.0,
        scan_limit: int = 5000,
    ) -> None:
        self._store = store
        self._queue = queue
        self._registry = registry
        self._reclaim = reclaim
        self._has_lease = has_lease
        self._default_timeout_s = default_timeout_s
        # A job is only "orphaned" once it has been leaseless for longer than
        # any legitimate gap between control-plane steps. Without this grace
        # period the reaper races the dispatch path and reclaims jobs that are
        # merely in transit -- the same class of bug as the Phase 3
        # wait_for_idle defect, arriving from the other direction.
        self._orphan_grace_s = orphan_grace_s
        self._scan_limit = scan_limit
        self.sweeps = 0
        self.last_report: ReaperReport | None = None

    # ------------------------------------------------------------------ #
    def sweep(self, now: datetime | None = None) -> ReaperReport:
        """One detection pass. Safe to call as often as you like."""
        now = now or utcnow()
        report = ReaperReport(swept_at=now)
        self.sweeps += 1

        live = self._live_jobs()
        unhealthy = self._unhealthy_workers()

        for job in live:
            # Order matters: a job on a dead worker that is *also* past its
            # timeout is reported as worker-lost, because that is the cause and
            # the timeout is a consequence. A trace that says "timed out" for a
            # job whose host vanished would send anyone reading it in the wrong
            # direction.
            if job.assigned_worker in unhealthy and job.status in ASSIGNED_STATUSES:
                status = unhealthy[job.assigned_worker]
                claim = Reclamation(
                    job=job,
                    event=JobEvent.WORKER_LOST,
                    reason=f"worker {job.assigned_worker} is {status.value}",
                    worker_id=job.assigned_worker,
                )
                if self._reclaim(claim):
                    report.worker_lost.append(job.job_id)
                continue

            if self._is_timed_out(job, now):
                claim = Reclamation(
                    job=job,
                    event=JobEvent.TIMEOUT,
                    reason=(
                        f"exceeded timeout_s={self._timeout_for(job):g} "
                        f"(running for {self._runtime_s(job, now):.1f}s)"
                    ),
                    worker_id=job.assigned_worker,
                )
                if self._reclaim(claim):
                    report.timed_out.append(job.job_id)
                continue

            if self._is_orphaned(job, now):
                claim = Reclamation(
                    job=job,
                    event=JobEvent.WORKER_LOST,
                    reason="no lease held by the control plane",
                    worker_id=job.assigned_worker,
                )
                if self._reclaim(claim):
                    report.orphaned.append(job.job_id)

        report.dead_lettered = self._sweep_dead_letters()
        self.last_report = report
        if report.total:
            log.info("reaper sweep: %s", report.as_dict())
        return report

    # ------------------------------------------------------------------ #
    # detection predicates
    # ------------------------------------------------------------------ #
    def _timeout_for(self, job: Job) -> float:
        return job.timeout_s or self._default_timeout_s

    @staticmethod
    def _runtime_s(job: Job, now: datetime) -> float:
        started = job.started_at or job.scheduled_at or job.created_at
        return (now - started).total_seconds()

    def _is_timed_out(self, job: Job, now: datetime) -> bool:
        """Wall-clock timeout, measured from the moment the worker accepted.

        Deliberately not measured from submission: queue wait is the
        scheduler's business and is already reported as a latency component
        (docs/experiments.md section 3). Charging queue time against a job's
        execution timeout would make the timeout a function of fleet load, and
        a heavily loaded fleet would start killing perfectly healthy work.
        """
        if job.status is not JobStatus.RUNNING or job.started_at is None:
            return False
        return self._runtime_s(job, now) > self._timeout_for(job)

    def _is_orphaned(self, job: Job, now: datetime) -> bool:
        """In a live state, on a healthy worker, but nobody holds its lease.

        This is the "control plane lost the plot" case: a crash or an exception
        between the store write and the lease bookkeeping. It cannot be
        detected by any event, because the whole problem is that no event is
        coming.
        """
        if job.status not in ASSIGNED_STATUSES:
            return False
        if self._has_lease(job.job_id):
            return False
        reference = job.started_at or job.scheduled_at or job.created_at
        return (now - reference).total_seconds() > self._orphan_grace_s

    # ------------------------------------------------------------------ #
    # collaborators
    # ------------------------------------------------------------------ #
    def _live_jobs(self) -> list[Job]:
        jobs: list[Job] = []
        for status in LIVE_STATUSES:
            jobs.extend(self._store.list_jobs(status=status, limit=self._scan_limit))
        return jobs

    def _unhealthy_workers(self) -> dict[str, WorkerStatus]:
        """Workers whose jobs must be reclaimed.

        DRAINING is excluded on purpose: a draining worker is administratively
        closed to *new* placement while its existing jobs run to completion.
        Reclaiming them would turn a graceful drain into a forced one.
        """
        return {
            w.worker_id: w.status
            for w in self._registry.snapshot().workers
            if w.status in (WorkerStatus.UNHEALTHY, WorkerStatus.DEAD)
        }

    def _sweep_dead_letters(self) -> list[str]:
        """Move poison messages to the DLQ and mark their jobs terminally FAILED.

        A message can exceed ``max_receive_count`` while its job sits in the
        store in a live state. Left alone the job would never resolve: the queue
        has stopped delivering it and no worker is running it. The DLQ is the
        backstop for exactly this, so the job's status has to follow the message.
        """
        moved = self._queue.sweep_expired()
        failed: list[str] = []
        for job_id in moved:
            job = self._store.get_job(job_id)
            if job is None or job.status in (JobStatus.COMPLETED, JobStatus.FAILED):
                continue
            claim = Reclamation(
                job=job,
                event=JobEvent.DEAD_LETTER,
                reason="dead-lettered: delivery count exhausted",
                worker_id=job.assigned_worker,
            )
            if self._reclaim(claim):
                failed.append(job_id)
        return failed
