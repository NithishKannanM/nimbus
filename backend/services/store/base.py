"""Control-plane state store.

Access patterns are enumerated in docs/architecture.md section 7; this
interface exposes exactly those and nothing that would require a scan.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from backend.models.decision import SchedulingDecision
from backend.models.enums import JobStatus
from backend.models.job import Job


class ConditionFailed(Exception):
    """A conditional write lost: the item was not in the expected state.

    Mirrors DynamoDB's ConditionalCheckFailedException so that concurrency bugs
    surface identically in both backends.
    """


@runtime_checkable
class StateStore(Protocol):
    # A1/A2 -- job by id, conditional update
    def put_job(self, job: Job) -> None: ...
    def get_job(self, job_id: str) -> Job | None: ...
    def update_job(self, job: Job, *, expect_status: JobStatus) -> None: ...

    # A3/A4 -- listings
    def list_jobs(
        self, *, status: JobStatus | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[Job]: ...
    def counts_by_status(self) -> dict[str, int]: ...

    # A5/A6/A7 -- decisions
    def put_decision(self, decision: SchedulingDecision) -> None: ...
    def list_decisions(self, *, job_id: str | None = None, limit: int = 50) -> list[SchedulingDecision]: ...
