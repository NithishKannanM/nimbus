from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import get_engine
from backend.api.schemas import SubmitBatchRequest, SubmitJobRequest, WorkloadInfo
from backend.models.enums import JobStatus
from backend.models.job import Job
from backend.services.engine import NimbusEngine
from worker.workloads.registry import REGISTRY, UnknownJobType

router = APIRouter(tags=["jobs"])


def _submit(engine: NimbusEngine, req: SubmitJobRequest) -> Job:
    try:
        return engine.submit(
            job_type=req.job_type,
            payload=req.payload,
            priority=req.priority,
            timeout_s=req.timeout_s,
            run_id=req.run_id,
        )
    except UnknownJobType as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post("/jobs", status_code=201, response_model=Job)
def submit_job(req: SubmitJobRequest, engine: NimbusEngine = Depends(get_engine)) -> Job:
    return _submit(engine, req)


@router.post("/jobs/batch", status_code=201, response_model=list[Job])
def submit_batch(
    req: SubmitBatchRequest, engine: NimbusEngine = Depends(get_engine)
) -> list[Job]:
    return [_submit(engine, item) for item in req.jobs]


@router.get("/jobs", response_model=list[Job])
def list_jobs(
    status: JobStatus | None = None,
    run_id: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    engine: NimbusEngine = Depends(get_engine),
) -> list[Job]:
    return engine.store.list_jobs(status=status, run_id=run_id, limit=limit)


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, engine: NimbusEngine = Depends(get_engine)) -> Job:
    job = engine.store.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"no such job: {job_id}")
    return job


@router.get("/workloads", response_model=list[WorkloadInfo])
def list_workloads() -> list[WorkloadInfo]:
    """The closed set of executable job types and their parameter schemas."""
    return [
        WorkloadInfo(
            job_type=spec.job_type,
            workload_class=spec.workload_class.value,
            parameters=spec.params_model.model_json_schema().get("properties", {}),
        )
        for spec in REGISTRY.values()
    ]
