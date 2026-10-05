"""HTTP worker agent -- the data plane on EC2.

This is ``LocalWorkerAgent`` behind an HTTP surface, and deliberately nothing
more. The accept/reject contract in ``agent.py`` was written to be the one an
HTTP handler would have (its docstring says so), so this module is a transport,
not a second implementation. Two implementations of "should this worker accept
this job" would eventually disagree, and the disagreement would show up as an
unexplainable difference between local and AWS benchmark results.

    POST /execute   dispatch. 202 accepted, 409 at capacity.
    GET  /health    liveness. The control plane's heartbeat source.
    GET  /telemetry observed host state, for the registry to ingest.
    POST /abandon   disown an execution the control plane has given up on.

**This host cannot reach the queue.** Its IAM role has no SQS and no DynamoDB
permissions at all (infra/iam.tf), so the "scheduler is the sole consumer"
invariant -- which every strategy comparison in this project depends on -- holds
because the worker is incapable of violating it, not because it chooses not to.

Results are POSTed back to the control plane rather than returned from
``/execute``. A job can run for a minute; holding an HTTP request open for its
duration would put the result at the mercy of every idle timeout between the two
hosts, and the scheduler would block waiting for it.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

from backend.models.job import Job
from worker.agent import LocalWorkerAgent
from worker.executor import ExecutionResult
from worker.telemetry import sample_worker

log = logging.getLogger("nimbus.worker.api")


class AbandonRequest(BaseModel):
    execution_id: str = Field(..., description="the execution to disown")


def result_payload(result: ExecutionResult) -> dict[str, Any]:
    """The wire form of a completion: ``ExecutionResultRequest`` on the other end."""
    return {
        "job_id": result.job_id,
        "execution_id": result.execution_id,
        "worker_id": result.worker_id,
        "ok": result.ok,
        "result": result.result,
        "error": result.error,
        "execution_time_ms": result.execution_time_ms,
        "cpu_units": result.cpu_units,
        "memory_mb": result.memory_mb,
    }


class ResultReporter:
    """Ships completions to the control plane, off the execution path.

    A background thread with a queue, rather than an inline POST from the
    executor's completion callback. That callback runs on the pool's result
    thread: a slow or unreachable control plane would stall it, and with it
    every subsequent completion on this worker -- turning a control-plane
    problem into a data-plane outage.

    Delivery is retried but not guaranteed. It does not need to be: the
    control plane's reaper detects a job whose result never arrives and
    reclaims it, so an undelivered result costs a re-execution rather than a
    lost job. That is the same trade the whole system already makes for a
    worker that dies mid-job.
    """

    def __init__(
        self,
        control_plane_url: str,
        *,
        worker_id: str,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._url = control_plane_url.rstrip("/")
        # Test seam: lets a suite route deliveries to an in-process app or a
        # MockTransport instead of the network.
        self._transport = transport
        self._worker_id = worker_id
        self._retries = retries
        self._queue: queue.Queue[ExecutionResult | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self.delivered = 0
        self.undelivered = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="nimbus-report", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._queue.put(None)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None

    def submit(self, result: ExecutionResult) -> None:
        self._queue.put(result)

    def _run(self) -> None:
        with httpx.Client(timeout=10.0, transport=self._transport) as client:
            while True:
                result = self._queue.get()
                if result is None:
                    return
                self._deliver(client, result)

    def _deliver(self, client: httpx.Client, result: ExecutionResult) -> None:
        payload = result_payload(result)
        for attempt in range(1, self._retries + 1):
            try:
                response = client.post(f"{self._url}/internal/results", json=payload)
                if response.status_code < 400:
                    self.delivered += 1
                    return
                log.warning(
                    "result %s rejected with %d", result.execution_id, response.status_code
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("result %s delivery attempt %d failed: %s",
                            result.execution_id, attempt, exc)
        self.undelivered += 1
        log.error(
            "giving up on %s; the reaper will reclaim the job", result.execution_id
        )


def create_app(
    *,
    worker_id: str | None = None,
    slots: int | None = None,
    control_plane_url: str | None = None,
    scratch_dir: str | None = None,
    agent: LocalWorkerAgent | None = None,
    reporter: ResultReporter | None = None,
) -> FastAPI:
    worker_id = worker_id or os.environ.get("NIMBUS_WORKER_ID", "worker-1")
    slots = slots or int(os.environ.get("NIMBUS_WORKER_SLOTS", "2"))
    control_plane_url = control_plane_url or os.environ.get(
        "NIMBUS_CONTROL_PLANE_URL", "http://127.0.0.1:8000"
    )
    scratch_dir = scratch_dir or os.environ.get("NIMBUS_SCRATCH_DIR")

    # ``agent`` and ``reporter`` are test seams. An injected agent is expected
    # to have been built with the injected reporter's ``submit`` as its sink.
    if reporter is None:
        reporter = ResultReporter(control_plane_url, worker_id=worker_id)
    if agent is None:
        agent = LocalWorkerAgent(
            worker_id, slots=slots, scratch_dir=scratch_dir, sink=reporter.submit
        )
    else:
        slots = getattr(agent, "slots", slots)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        agent.start()
        reporter.start()
        log.info("worker %s up with %d slots", worker_id, slots)
        try:
            yield
        finally:
            agent.stop(wait=False)
            reporter.stop()

    app = FastAPI(
        title=f"Nimbus worker {worker_id}", version="0.5.0-phase5", lifespan=lifespan
    )
    app.state.agent = agent
    app.state.reporter = reporter

    @app.post("/execute", status_code=202)
    def execute(job: Job, response: Response) -> dict[str, Any]:
        """Accept or reject one dispatch.

        409 means "at capacity", and it is a *fast* signal: the scheduler
        re-scores excluding this worker without a queue round-trip
        (docs/architecture.md section 3.3, rule 3). It is not an error and it
        does not consume the job's retry budget.
        """
        accepted = agent.try_execute(job)
        if not accepted:
            raise HTTPException(
                status_code=409,
                detail=f"{worker_id} at capacity: {agent.active_jobs}/{slots}",
            )
        response.status_code = 202
        return {
            "accepted": True,
            "worker_id": worker_id,
            "execution_id": job.execution_id,
            "active_jobs": agent.active_jobs,
        }

    @app.post("/abandon")
    def abandon(req: AbandonRequest) -> dict[str, Any]:
        """Disown an execution the control plane has already reclaimed.

        Frees the slot and suppresses the eventual result, so a late completion
        cannot be credited to an attempt that has been replaced.
        """
        return {"abandoned": agent.abandon(req.execution_id)}

    @app.get("/health")
    def health() -> dict[str, Any]:
        """Liveness. Carries no resource information, on purpose.

        Heartbeats and telemetry are separate mechanisms (worker/heartbeat.py):
        liveness is a correctness signal every strategy needs, while resource
        observation is the experimental variable. Merging them would mean the
        telemetry-OFF control arm had no healthy fleet.
        """
        return {"ok": True, "worker_id": worker_id, "crashed": agent.crashed}

    @app.get("/telemetry")
    def telemetry() -> dict[str, Any]:
        """Observed host state, scoped to this worker's own processes.

        On EC2 a worker *is* the host, so unlike the local fleet there is no
        core-affinity trick needed to make per-worker CPU an independent signal
        -- which is precisely why docs/limitations.md section 3 says the local
        results are directional and the headline experiment has to be re-run
        here.
        """
        sample = sample_worker(worker_id, pids=agent.child_pids(),
                               active_jobs=agent.active_jobs)
        return sample.model_dump(mode="json")

    @app.get("/stats")
    def stats() -> dict[str, Any]:
        return {
            **agent.stats(),
            "results_delivered": reporter.delivered,
            "results_undelivered": reporter.undelivered,
        }

    return app


app = create_app()
