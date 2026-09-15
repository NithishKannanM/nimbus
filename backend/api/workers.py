from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import get_engine
from backend.api.schemas import TelemetryToggleRequest
from backend.models.telemetry import WorkerTelemetry
from backend.models.worker import WorkerState
from backend.services.engine import NimbusEngine

router = APIRouter(tags=["workers"])


@router.get("/workers", response_model=list[WorkerState])
def list_workers(engine: NimbusEngine = Depends(get_engine)) -> list[WorkerState]:
    """Full worker state: DECLARED capacity, Nimbus RESERVATIONS, and OBSERVED
    telemetry when collection is enabled. The three are kept separate here for
    the same reason they are separate in the scheduler."""
    return list(engine.fleet().workers)


@router.get("/workers/{worker_id}", response_model=WorkerState)
def get_worker(worker_id: str, engine: NimbusEngine = Depends(get_engine)) -> WorkerState:
    state = engine.fleet().by_id(worker_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
    return state


@router.get("/workers/{worker_id}/telemetry", response_model=list[WorkerTelemetry])
def worker_telemetry(
    worker_id: str,
    limit: int = Query(60, ge=1, le=300),
    engine: NimbusEngine = Depends(get_engine),
) -> list[WorkerTelemetry]:
    if engine.fleet().by_id(worker_id) is None:
        raise HTTPException(status_code=404, detail=f"no such worker: {worker_id}")
    return engine.registry.telemetry_history(worker_id, limit=limit)


@router.get("/telemetry", tags=["telemetry"])
def telemetry_status(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    fleet = engine.fleet()
    return {
        "enabled": engine.telemetry_enabled,
        "interval_s": engine.config.telemetry_interval_s,
        "collector_running": engine.telemetry.running,
        "overhead": engine.telemetry_overhead(),
        "heartbeats_emitted": engine.heartbeats.beats,
        "current": {
            w.worker_id: (w.observed.model_dump() if w.observed else None) for w in fleet.workers
        },
    }


@router.put("/telemetry", tags=["telemetry"])
def set_telemetry(
    req: TelemetryToggleRequest, engine: NimbusEngine = Depends(get_engine)
) -> dict[str, Any]:
    """Toggle observed-state collection.

    No implemented placement policy can read telemetry, so this must not change
    any placement decision. It exists as the control arm of the Phase 2
    telemetry-overhead experiment.
    """
    enabled = engine.set_telemetry(req.enabled)
    return {"enabled": enabled, "collector_running": engine.telemetry.running}
