"""Control-plane endpoints that remote workers call.

Mounted only when ``NIMBUS_BACKEND=aws``, because in local mode the workers are
in-process and push their results onto a queue directly -- there is no network
hop to serve, and an endpoint that exists but is never used is a surface with no
purpose.

Access control is the security group (infra/networking.tf): port 8000 accepts
only ``var.admin_cidr``, and the workers reach it from inside the VPC. There is
no authentication here because Nimbus has none anywhere before Phase 7
(docs/limitations.md section 5), and pretending otherwise on this one router
would be worse than stating it.

What *is* enforced here is that a reported result cannot be trusted to be
current. The worker says which execution it ran; the engine checks that against
the job's own attempt before writing anything. A late result from a superseded
attempt is discarded, which is the third of the four idempotency guards in
docs/architecture.md section 8 and the reason a job may execute more than once
but completes exactly once.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.api.deps import get_engine
from backend.api.schemas import ExecutionResultRequest
from backend.services.engine import NimbusEngine
from worker.executor import ExecutionResult

router = APIRouter(prefix="/internal", tags=["internal"])


@router.post("/results", status_code=202)
def report_result(
    req: ExecutionResultRequest, engine: NimbusEngine = Depends(get_engine)
) -> dict[str, Any]:
    """Accept a completion from a remote worker.

    Always 202, never a verdict. The worker has already done the work and has
    nothing useful to do with a rejection, and making it wait for the store
    write would put a DynamoDB round-trip on the worker's result path. Whether
    the result was accepted or discarded as stale is the control plane's
    business and shows up in ``/admin/reliability`` as ``stale_results``.
    """
    engine.report_result(
        ExecutionResult(
            job_id=req.job_id,
            execution_id=req.execution_id,
            worker_id=req.worker_id,
            ok=req.ok,
            result=req.result,
            error=req.error,
            execution_time_ms=req.execution_time_ms,
            cpu_units=req.cpu_units,
            memory_mb=req.memory_mb,
        )
    )
    return {"received": True, "execution_id": req.execution_id}
