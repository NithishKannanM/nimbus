"""Job model and the authoritative job state machine.

The transition table here is the single definition of legal job lifecycle moves
(docs/architecture.md section 4). Nothing else in the codebase may set
``Job.status`` directly.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from backend.models.enums import JobEvent, JobStatus, WorkloadClass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def new_job_id() -> str:
    return f"job-{uuid.uuid4().hex[:12]}"


class IllegalTransition(Exception):
    """Raised when a (status, event) pair is not in the transition table."""

    def __init__(self, status: JobStatus, event: JobEvent) -> None:
        super().__init__(f"illegal transition: {status} --{event}--> ?")
        self.status = status
        self.event = event


#: (current status, event) -> next status.  Exhaustively unit-tested.
TRANSITIONS: dict[tuple[JobStatus, JobEvent], JobStatus] = {
    (JobStatus.QUEUED, JobEvent.SCHEDULE): JobStatus.SCHEDULED,
    (JobStatus.SCHEDULED, JobEvent.ACCEPTED): JobStatus.RUNNING,
    (JobStatus.SCHEDULED, JobEvent.DISPATCH_REJECTED): JobStatus.QUEUED,
    (JobStatus.SCHEDULED, JobEvent.WORKER_LOST): JobStatus.RETRYING,
    (JobStatus.SCHEDULED, JobEvent.FAILED): JobStatus.RETRYING,
    (JobStatus.RUNNING, JobEvent.COMPLETED): JobStatus.COMPLETED,
    (JobStatus.RUNNING, JobEvent.FAILED): JobStatus.RETRYING,
    (JobStatus.RUNNING, JobEvent.TIMEOUT): JobStatus.TIMED_OUT,
    (JobStatus.RUNNING, JobEvent.WORKER_LOST): JobStatus.RETRYING,
    (JobStatus.RETRYING, JobEvent.REQUEUE): JobStatus.QUEUED,
    (JobStatus.RETRYING, JobEvent.EXHAUSTED): JobStatus.FAILED,
    (JobStatus.TIMED_OUT, JobEvent.REQUEUE): JobStatus.QUEUED,
    (JobStatus.TIMED_OUT, JobEvent.EXHAUSTED): JobStatus.FAILED,
    # Dead-lettering is terminal from every live state, including QUEUED.
    # It is not a retry outcome and does not consult the retry budget: the
    # queue has stopped delivering the message, so no future attempt can
    # happen regardless of how many attempts the job has left.
    (JobStatus.QUEUED, JobEvent.DEAD_LETTER): JobStatus.FAILED,
    (JobStatus.SCHEDULED, JobEvent.DEAD_LETTER): JobStatus.FAILED,
    (JobStatus.RUNNING, JobEvent.DEAD_LETTER): JobStatus.FAILED,
    (JobStatus.RETRYING, JobEvent.DEAD_LETTER): JobStatus.FAILED,
    (JobStatus.TIMED_OUT, JobEvent.DEAD_LETTER): JobStatus.FAILED,
}


def next_status(status: JobStatus, event: JobEvent) -> JobStatus:
    try:
        return TRANSITIONS[(status, event)]
    except KeyError:
        raise IllegalTransition(status, event) from None


class Job(BaseModel):
    job_id: str = Field(default_factory=new_job_id)
    job_type: str
    workload_class: WorkloadClass
    priority: int = 3
    status: JobStatus = JobStatus.QUEUED
    payload: dict[str, Any] = Field(default_factory=dict)

    input_location: str | None = None
    output_location: str | None = None
    assigned_worker: str | None = None

    attempts: int = 0
    max_attempts: int = 3
    timeout_s: float = 300.0

    # Declared requirements -- the ECS-style inputs the resource-aware and
    # adaptive schedulers filter on (docs/scheduling.md section 3).
    cpu_units: int = 512
    memory_mb: int = 256

    created_at: datetime = Field(default_factory=utcnow)
    scheduled_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    execution_time_ms: float | None = None

    error: str | None = None
    result: dict[str, Any] | None = None
    run_id: str | None = None

    @property
    def execution_id(self) -> str:
        """Idempotency token for the current attempt (architecture.md section 8)."""
        return f"{self.job_id}#{self.attempts}"

    def apply(self, event: JobEvent, *, at: datetime | None = None) -> "Job":
        """Return a copy advanced by ``event``.  Raises IllegalTransition."""
        now = at or utcnow()
        target = next_status(self.status, event)
        patch: dict[str, Any] = {"status": target}

        if event is JobEvent.SCHEDULE:
            patch["scheduled_at"] = now
        elif event is JobEvent.ACCEPTED:
            patch["started_at"] = now
        elif event is JobEvent.COMPLETED:
            patch["completed_at"] = now
            start = self.started_at or self.created_at
            patch["execution_time_ms"] = (now - start).total_seconds() * 1000.0
        elif event in (JobEvent.EXHAUSTED, JobEvent.DEAD_LETTER):
            patch["completed_at"] = now
        elif event is JobEvent.REQUEUE:
            patch["assigned_worker"] = None
            patch["scheduled_at"] = None
            patch["started_at"] = None
        elif event is JobEvent.DISPATCH_REJECTED:
            patch["assigned_worker"] = None
            patch["scheduled_at"] = None

        return self.model_copy(update=patch)

    def wait_seconds(self, now: datetime | None = None) -> float:
        return ((now or utcnow()) - self.created_at).total_seconds()
