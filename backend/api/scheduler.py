from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from backend.api.deps import get_engine
from backend.api.schemas import StrategyRequest
from backend.models.decision import SchedulingDecision
from backend.services.engine import NimbusEngine
from scheduler import STRATEGIES

router = APIRouter(prefix="/scheduler", tags=["scheduler"])


@router.get("")
def get_scheduler(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    """Active strategy plus its two halves.

    Ordering and placement are reported separately because they are separate
    decisions: a benchmark holds one constant while varying the other.
    """
    return {
        "strategy": engine.scheduler.name,
        "ordering_policy": engine.scheduler.ordering.name,
        "placement_policy": engine.scheduler.placement.name,
        "available": sorted(STRATEGIES),
        **engine.stats(),
    }


@router.put("")
def set_scheduler(req: StrategyRequest, engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    try:
        name = engine.set_strategy(req.strategy)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return {
        "strategy": name,
        "ordering_policy": engine.scheduler.ordering.name,
        "placement_policy": engine.scheduler.placement.name,
        "available": sorted(STRATEGIES),
    }


@router.get("/comparison")
def get_comparison(engine: NimbusEngine = Depends(get_engine)) -> dict[str, Any]:
    """Live per-strategy figures, grouped by the strategy that placed each job.

    A demonstration, not evidence: strategies ran at different times under
    different load. The benchmark archive is the controlled comparison.
    """
    return engine.comparison()


@router.get("/decisions", response_model=list[SchedulingDecision])
def list_decisions(
    job_id: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    engine: NimbusEngine = Depends(get_engine),
) -> list[SchedulingDecision]:
    return engine.decisions(job_id=job_id, limit=limit)
