"""Administrative and failure-simulation endpoints.

Kept on a separate router, mounted only when ``NIMBUS_ENABLE_ADMIN`` is set,
because every route here is destructive by design. ``POST
/admin/workers/{id}/fail`` kills a worker's executor and loses whatever was
running on it. That is exactly what the Phase 4 exit criterion needs to
exercise -- detect, requeue, complete elsewhere -- and exactly what must not be
reachable on a listener anyone else can talk to (docs/limitations.md section 5).

Nimbus is unauthenticated by design through Phase 7, so "not mounted" is the
only access control available. A flag that has to be deliberately set is a
weaker guarantee than authentication, and it is the honest one to state.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import get_engine
from backend.api.schemas import WorkerFailureRequest
from backend.services.engine import NimbusEngine

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("/reliability")
def reliability(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    """Retries, timeouts, reclamations, duplicates and dead letters.

    Duplicate executions are published rather than logged: a system claiming
    exactly-once completion under at-least-once delivery has to show its
    duplicate rate, or the claim is untested (docs/architecture.md section 8).
    """
    return engine.reliability()


@router.post("/reaper/sweep")
def sweep(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    """Force one reaper pass instead of waiting for its interval."""
    return engine.sweep().as_dict()


@router.post("/workers/{worker_id}/fail")
def fail_worker(
    worker_id: str,
    req: WorkerFailureRequest | None = None,
    engine: NimbusEngine = Depends(get_engine),
) -> dict[str, Any]:
    """Fail a worker the way a real one fails: without telling anyone.

    The worker stops heartbeating; the control plane is not informed and has to
    infer the loss. ``hard`` (the default) also destroys the executor, losing
    in-flight work. ``hard=false`` is a partition: the jobs keep running but
    nothing can hear the worker, so they are reclaimed and re-placed while the
    original executions continue -- the duplicate-execution scenario, produced
    honestly rather than mocked.
    """
    try:
        return engine.fail_worker(worker_id, hard=(req.hard if req else True))
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}") from None


@router.post("/workers/{worker_id}/recover")
def recover_worker(worker_id: str, engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    try:
        return engine.recover_worker(worker_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}") from None


@router.post("/workers/{worker_id}/drain")
def drain_worker(
    worker_id: str,
    undo: bool = Query(False, description="return the worker to HEALTHY"),
    engine: NimbusEngine = Depends(get_engine),
) -> dict[str, Any]:
    """Close a worker to new placement without failing it.

    Draining is not a failure and the reaper leaves draining workers alone:
    in-flight jobs finish where they are. Reclaiming them would turn a graceful
    drain into a forced one.
    """
    try:
        return engine.undrain_worker(worker_id) if undo else engine.drain_worker(worker_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}") from None


@router.get("/dlq")
def dead_letters(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    """The poison-message backstop.

    A job id here means the queue stopped delivering it. Its job record is
    terminally FAILED, because a dead-lettered message with a live job record
    is a job that can never resolve.
    """
    job_ids = engine.queue.dead_letters()
    return {
        "count": len(job_ids),
        "job_ids": job_ids,
        "jobs": [
            job.model_dump(mode="json")
            for job in (engine.store.get_job(j) for j in job_ids)
            if job is not None
        ],
    }
