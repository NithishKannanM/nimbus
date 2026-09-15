from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from worker.workloads.registry import JOB_TYPES


class SubmitJobRequest(BaseModel):
    job_type: str = Field(..., description=f"one of: {', '.join(JOB_TYPES)}")
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = Field(3, ge=1, le=4, description="1=Critical 2=High 3=Normal 4=Low")
    timeout_s: float | None = Field(None, gt=0, le=3600)
    run_id: str | None = None


class SubmitBatchRequest(BaseModel):
    jobs: list[SubmitJobRequest] = Field(..., min_length=1, max_length=1000)


class StrategyRequest(BaseModel):
    strategy: str


class WorkloadInfo(BaseModel):
    job_type: str
    workload_class: str
    parameters: dict[str, Any]


class TelemetryToggleRequest(BaseModel):
    enabled: bool


class WorkerFailureRequest(BaseModel):
    hard: bool = Field(
        True,
        description=(
            "true: crash the executor, losing in-flight work. "
            "false: partition it -- the jobs keep running but the control "
            "plane can no longer hear the worker."
        ),
    )


class ExecutionResultRequest(BaseModel):
    """A completion reported by a remote worker (worker/api.py).

    The wire form of ``worker.executor.ExecutionResult``. Nothing here is
    trusted to be current: ``execution_id`` is checked against the job's own
    attempt before anything is written, so a late result from a superseded
    attempt is discarded rather than credited (docs/architecture.md section 8).
    """

    job_id: str
    execution_id: str
    worker_id: str
    ok: bool
    result: dict[str, Any] | None = None
    error: str | None = None
    execution_time_ms: float = 0.0
    cpu_units: int = 0
    memory_mb: int = 0


class BenchmarkSubmitRequest(BaseModel):
    """One sweep: a profile crossed with a set of strategies, repeated.

    ``seed`` is shared across strategies on purpose. Every arm then runs a
    literally identical job sequence, so a difference between arms is
    attributable to placement rather than to the workload
    (docs/experiments.md section 2.1).
    """

    profile: str = Field(..., description="workload profile; see GET /benchmarks/profiles")
    strategies: list[str] = Field(..., min_length=1, max_length=8)
    job_count: int = Field(60, ge=1, le=2000)
    #: docs/experiments.md section 2.3 requires >= 5 for an inferential claim.
    #: One is permitted here because a single run is a legitimate smoke test --
    #: it is the *reporting* of a single run as evidence that is not.
    trials: int = Field(1, ge=1, le=50)
    seed: int = 1337
    telemetry: bool = True
    size_scale: float = Field(1.0, gt=0.0, le=10.0)
    worker_count: int | None = Field(None, ge=1, le=16)
    worker_slots: int | None = Field(None, ge=1, le=16)
    warmup_jobs: int = Field(3, ge=0, le=50)
    timeout_s: float = Field(600.0, gt=0, le=7200)
    apply_pressure: bool = Field(
        True,
        description=(
            "Apply the profile's external host pressure. Turning it off on a "
            "pressure profile removes the entire phenomenon under study."
        ),
    )
