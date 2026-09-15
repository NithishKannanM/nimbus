from __future__ import annotations

from enum import StrEnum


class WorkloadClass(StrEnum):
    CPU_BOUND = "CPU_BOUND"
    MEMORY_BOUND = "MEMORY_BOUND"
    IO_BOUND = "IO_BOUND"
    LIGHTWEIGHT = "LIGHTWEIGHT"


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"
    TIMED_OUT = "TIMED_OUT"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})


class JobEvent(StrEnum):
    SCHEDULE = "schedule"
    ACCEPTED = "accepted"
    DISPATCH_REJECTED = "dispatch_rejected"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    WORKER_LOST = "worker_lost"
    REQUEUE = "requeue"
    EXHAUSTED = "exhausted"
    #: The queue gave up delivering this message. Terminal from any live
    #: state, including QUEUED -- a poison message is never retried, and the
    #: job's status has to follow the message or it would sit live forever
    #: with nothing left to deliver it.
    DEAD_LETTER = "dead_letter"


class WorkerStatus(StrEnum):
    HEALTHY = "HEALTHY"
    UNHEALTHY = "UNHEALTHY"
    DRAINING = "DRAINING"
    DEAD = "DEAD"


class Priority(int):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4
