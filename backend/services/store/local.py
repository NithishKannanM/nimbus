"""In-memory state store with DynamoDB-equivalent conditional-write semantics."""

from __future__ import annotations

import threading
from collections import defaultdict, deque

from backend.models.decision import SchedulingDecision
from backend.models.enums import JobStatus
from backend.models.job import Job
from backend.services.store.base import ConditionFailed


class LocalStateStore:
    def __init__(self, *, decision_history: int = 5000) -> None:
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []  # insertion order, for newest-first listing
        self._decisions: deque[SchedulingDecision] = deque(maxlen=decision_history)
        self._by_job: dict[str, list[SchedulingDecision]] = defaultdict(list)

    def put_job(self, job: Job) -> None:
        with self._lock:
            if job.job_id in self._jobs:
                raise ConditionFailed(f"{job.job_id} already exists")
            self._jobs[job.job_id] = job
            self._order.append(job.job_id)

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update_job(self, job: Job, *, expect_status: JobStatus) -> None:
        with self._lock:
            current = self._jobs.get(job.job_id)
            if current is None:
                raise ConditionFailed(f"{job.job_id} does not exist")
            if current.status != expect_status:
                raise ConditionFailed(
                    f"{job.job_id}: expected status {expect_status}, found {current.status}"
                )
            self._jobs[job.job_id] = job

    def list_jobs(
        self, *, status: JobStatus | None = None, run_id: str | None = None, limit: int = 100
    ) -> list[Job]:
        with self._lock:
            out: list[Job] = []
            for job_id in reversed(self._order):  # newest first
                job = self._jobs[job_id]
                if status is not None and job.status != status:
                    continue
                if run_id is not None and job.run_id != run_id:
                    continue
                out.append(job)
                if len(out) >= limit:
                    break
            return out

    def counts_by_status(self) -> dict[str, int]:
        with self._lock:
            counts = {s.value: 0 for s in JobStatus}
            for job in self._jobs.values():
                counts[job.status.value] += 1
            return counts

    def put_decision(self, decision: SchedulingDecision) -> None:
        with self._lock:
            self._decisions.append(decision)
            self._by_job[decision.job_id].append(decision)

    def list_decisions(
        self, *, job_id: str | None = None, limit: int = 50
    ) -> list[SchedulingDecision]:
        with self._lock:
            source = self._by_job.get(job_id, []) if job_id else list(self._decisions)
            return list(reversed(source))[:limit]
