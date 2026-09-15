"""Small constructors shared by tests and scripts."""

from __future__ import annotations

from backend.models.enums import WorkloadClass
from backend.models.job import Job


def make_job(
    job_type: str = "prime_count",
    workload_class: WorkloadClass = WorkloadClass.CPU_BOUND,
    **overrides,
) -> Job:
    return Job(job_type=job_type, workload_class=workload_class, **overrides)
