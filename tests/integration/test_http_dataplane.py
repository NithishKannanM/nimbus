"""The HTTP data plane end to end, in one process.

The engine drives real ``RemoteWorkerClient`` instances; each one talks, via an
``httpx.MockTransport`` that forwards into a starlette ``TestClient``, to a real
worker FastAPI app (worker/api.py). The worker's ``ResultReporter`` posts back
through a second bridge into a control-plane app that mounts only the internal
router, wired to the same engine. No sockets, but every serialisation boundary
and both HTTP directions are real.

Most tests use a scripted fake agent and drive ``engine.tick()`` directly, so
nothing depends on a scheduler thread's timing. The Linux-only test swaps in
real ``LocalWorkerAgent`` process pools.
"""

from __future__ import annotations

import multiprocessing
import time
from contextlib import ExitStack

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api.deps import get_engine
from backend.api.internal import router as internal_router
from backend.config import NimbusConfig
from backend.models.enums import JobStatus, WorkerStatus
from backend.models.job import Job
from backend.services.engine import NimbusEngine
from worker.api import ResultReporter, create_app
from worker.executor import ExecutionResult

DEADLINE_S = 15.0


def wait_until(predicate, *, timeout_s: float = DEADLINE_S, interval_s: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return predicate()


def forward(client: TestClient, request: httpx.Request) -> httpx.Response:
    """Replay an httpx request against a TestClient and hand back its response."""
    target = request.url.raw_path.decode("ascii")
    response = client.request(
        request.method, target, content=request.read(),
        headers={"content-type": request.headers.get("content-type", "application/json")},
    )
    return httpx.Response(response.status_code, content=response.content,
                          headers={"content-type": response.headers["content-type"]})


class FakeAgent:
    """Scripted stand-in for LocalWorkerAgent. Holds jobs until told to finish."""

    def __init__(self, worker_id: str, *, slots: int = 2) -> None:
        self.worker_id = worker_id
        self.slots = slots
        self.crashed = False
        self.reporter: ResultReporter | None = None
        self.accept = True
        self.received: list[Job] = []
        self.abandoned: list[str] = []
        self._held: list[Job] = []

    @property
    def active_jobs(self) -> int:
        return len(self._held)

    def start(self) -> None: ...
    def stop(self, *, wait: bool = True) -> None: ...

    def try_execute(self, job: Job) -> bool:
        self.received.append(job)
        if not self.accept or len(self._held) >= self.slots:
            return False
        self._held.append(job)
        return True

    def abandon(self, execution_id: str) -> bool:
        self.abandoned.append(execution_id)
        self._held = [j for j in self._held if j.execution_id != execution_id]
        return True

    def child_pids(self) -> tuple[int, ...]:
        return ()

    def stats(self) -> dict:
        return {"active_jobs": self.active_jobs, "slots": self.slots, "crashed": self.crashed}

    def result_for(self, job: Job, *, primes: int = 168) -> ExecutionResult:
        return ExecutionResult(
            job_id=job.job_id, execution_id=job.execution_id, worker_id=self.worker_id,
            ok=True, result={"primes": primes}, error=None, execution_time_ms=3.0,
            cpu_units=job.cpu_units, memory_mb=job.memory_mb,
        )

    def finish(self, job: Job) -> ExecutionResult:
        assert self.reporter is not None
        self._held = [j for j in self._held if j.execution_id != job.execution_id]
        result = self.result_for(job)
        self.reporter.submit(result)
        return result


class Harness:
    """Engine + control-plane app + one worker app per endpoint, all bridged."""

    def __init__(self, tmp_path, worker_ids: tuple[str, ...], *, agents: bool = True,
                 slots: int = 2, **overrides) -> None:
        self.stack = ExitStack()
        self.worker_ids = worker_ids
        self.unhealthy: set[str] = set()
        self.worker_clients: dict[str, TestClient] = {}
        self.agents: dict[str, FakeAgent] = {}
        self.reporters: dict[str, ResultReporter] = {}
        self._use_fakes = agents
        self._slots = slots

        cfg = dict(
            backend="local",
            worker_transport="http",
            worker_endpoints=",".join(f"{w}=http://{w}.test:8080" for w in worker_ids),
            worker_slots=slots,
            artifact_dir=str(tmp_path / "artifacts"),
            scheduler_tick_s=0.01,
            max_attempts=3,
        )
        cfg.update(overrides)
        self.engine = NimbusEngine(NimbusConfig(**cfg), http_transport=self.worker_transport)

        cp_app = FastAPI()
        cp_app.include_router(internal_router)
        cp_app.dependency_overrides[get_engine] = lambda: self.engine
        self.control_plane = TestClient(cp_app)
        control_transport = httpx.MockTransport(lambda r: forward(self.control_plane, r))

        for worker_id in worker_ids:
            reporter = ResultReporter("http://control-plane.test", worker_id=worker_id,
                                      transport=control_transport)
            kwargs: dict = dict(worker_id=worker_id, reporter=reporter)
            if agents:
                agent = FakeAgent(worker_id, slots=slots)
                agent.reporter = reporter
                self.agents[worker_id] = agent
                kwargs["agent"] = agent
            else:
                kwargs.update(slots=slots, control_plane_url="http://control-plane.test")
            # The context manager runs the lifespan: agent.start, reporter.start.
            self.worker_clients[worker_id] = self.stack.enter_context(
                TestClient(create_app(**kwargs))
            )
            self.reporters[worker_id] = reporter

    def worker_transport(self, worker_id: str) -> httpx.BaseTransport:
        client = self.worker_clients[worker_id]

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/health" and worker_id in self.unhealthy:
                return httpx.Response(500, json={"detail": "sick"})
            return forward(client, request)

        return httpx.MockTransport(handler)

    def __enter__(self) -> "Harness":
        self.engine._start_fleet()
        return self

    def __exit__(self, *exc) -> None:
        try:
            self.engine.stop(wait=False)
        finally:
            try:
                self.stack.close()  # worker lifespans stop their reporters
            finally:
                self.control_plane.close()

    def tick_until(self, predicate, *, timeout_s: float = DEADLINE_S) -> bool:
        return wait_until(lambda: (self.engine.tick(), predicate())[1], timeout_s=timeout_s)

    def job(self, job_id: str) -> Job:
        job = self.engine.store.get_job(job_id)
        assert job is not None
        return job


# --------------------------------------------------------------------------- #
# fake-agent tests: deterministic, no process pools
# --------------------------------------------------------------------------- #
def test_job_completes_over_http_both_directions_and_releases_its_reservation(tmp_path):
    with Harness(tmp_path, ("worker-a",)) as h:
        job = h.engine.submit(job_type="prime_count", payload={"limit": 1000})
        h.engine.tick()  # dispatch: control plane -> worker over HTTP

        running = h.job(job.job_id)
        assert running.status is JobStatus.RUNNING
        assert running.assigned_worker == "worker-a"
        agent = h.agents["worker-a"]
        assert [j.execution_id for j in agent.received] == [running.execution_id]
        assert h.engine.registry.reserved_executions("worker-a") == (running.execution_id,)

        agent.finish(running)  # result: worker -> control plane over HTTP
        assert h.tick_until(lambda: h.job(job.job_id).status is JobStatus.COMPLETED)

        done = h.job(job.job_id)
        assert done.result == {"primes": 168}
        assert done.attempts == 1
        assert h.engine.registry.reserved_executions("worker-a") == ()
        assert h.engine._workers["worker-a"].active_jobs == 0
        assert wait_until(lambda: h.reporters["worker-a"].delivered == 1)
        assert h.engine._stats["completed"] == 1
        assert h.engine._stats["stale_results"] == 0


def test_a_409_from_a_worker_at_capacity_costs_no_attempt(tmp_path):
    with Harness(tmp_path, ("worker-a",)) as h:
        h.agents["worker-a"].accept = False
        job = h.engine.submit(job_type="prime_count", payload={"limit": 1000})
        h.engine.tick()

        stored = h.job(job.job_id)
        assert stored.status is JobStatus.QUEUED
        assert stored.attempts == 0
        assert h.engine._stats["dispatch_rejected"] == 1
        assert h.engine._workers["worker-a"].rejected == 1
        assert h.engine._workers["worker-a"].dispatch_errors == 0
        assert h.engine.registry.reserved_executions("worker-a") == ()

        # Once the worker has room again the same job runs on its first attempt.
        h.agents["worker-a"].accept = True
        assert h.tick_until(lambda: h.job(job.job_id).status is JobStatus.RUNNING)
        assert h.job(job.job_id).attempts == 1


def test_a_full_worker_rejects_via_the_real_409_path_and_recovers(tmp_path):
    with Harness(tmp_path, ("worker-a",), slots=1) as h:
        first = h.engine.submit(job_type="prime_count", payload={"limit": 1000})
        h.engine.tick()
        assert h.job(first.job_id).status is JobStatus.RUNNING

        # The control plane thinks the slot is taken, so make the worker the one
        # that says no: forget the reservation but keep the fake's held job.
        held = h.job(first.job_id)
        h.engine.registry.release("worker-a", execution_id=held.execution_id)
        second = h.engine.submit(job_type="prime_count", payload={"limit": 1000})
        h.engine.tick()
        assert h.job(second.job_id).attempts == 0
        assert h.engine._stats["dispatch_rejected"] >= 1
        assert h.engine._workers["worker-a"].rejected >= 1


def test_a_duplicate_result_is_counted_stale_and_does_not_double_complete(tmp_path):
    with Harness(tmp_path, ("worker-a",)) as h:
        job = h.engine.submit(job_type="prime_count", payload={"limit": 1000})
        h.engine.tick()
        running = h.job(job.job_id)
        agent = h.agents["worker-a"]

        agent.finish(running)
        assert h.tick_until(lambda: h.job(job.job_id).status is JobStatus.COMPLETED)
        assert h.engine._stats["stale_results"] == 0

        agent.reporter.submit(agent.result_for(running))  # the same result again
        assert wait_until(lambda: h.reporters["worker-a"].delivered == 2)
        assert h.tick_until(lambda: h.engine._stats["stale_results"] == 1)

        assert h.engine._stats["completed"] == 1
        assert h.job(job.job_id).status is JobStatus.COMPLETED
        assert h.engine.registry.reserved_executions("worker-a") == ()
        assert h.engine.reliability()["stale_results"] == 1


def test_reaper_timeout_sends_abandon_to_the_worker_and_the_job_is_retried(tmp_path):
    with Harness(tmp_path, ("worker-a",)) as h:
        job = h.engine.submit(job_type="prime_count", payload={"limit": 1000},
                              timeout_s=0.05)
        h.engine.tick()
        first = h.job(job.job_id)
        assert first.status is JobStatus.RUNNING
        agent = h.agents["worker-a"]

        time.sleep(0.15)  # past timeout_s; the fake never finishes
        report = h.engine.sweep()
        assert report.timed_out == [job.job_id]
        assert h.engine._stats["timed_out"] == 1

        # /abandon is fire-and-forget on a background thread.
        assert wait_until(lambda: first.execution_id in agent.abandoned)
        assert agent.active_jobs == 0
        assert h.engine.registry.reserved_executions("worker-a") == ()
        assert h.job(job.job_id).status is not JobStatus.COMPLETED


def test_a_failing_health_probe_makes_the_worker_unhealthy_then_recovers(tmp_path):
    with Harness(tmp_path, ("worker-a", "worker-b"), heartbeat_timeout_s=0.1,
                 eviction_timeout_s=60.0) as h:
        def status(worker_id: str) -> WorkerStatus:
            return h.engine.registry.snapshot().by_id(worker_id).status

        assert h.engine.heartbeats.beat_once() == 2
        assert status("worker-a") is WorkerStatus.HEALTHY

        h.unhealthy.add("worker-b")  # /health now returns 500
        deadline = time.monotonic() + DEADLINE_S
        while status("worker-b") is not WorkerStatus.UNHEALTHY and time.monotonic() < deadline:
            time.sleep(0.02)
            h.engine.heartbeats.beat_once()  # worker-a keeps beating, worker-b cannot
        assert status("worker-b") is WorkerStatus.UNHEALTHY
        assert status("worker-a") is WorkerStatus.HEALTHY
        assert h.engine._workers["worker-b"].probe_failures >= 1

        h.unhealthy.clear()
        h.engine.heartbeats.beat_once()
        assert status("worker-b") is WorkerStatus.HEALTHY


def test_remote_telemetry_is_ingested_through_the_worker_app(tmp_path):
    with Harness(tmp_path, ("worker-a",)) as h:
        samples = h.engine.telemetry.collect_once()
        assert [s.worker_id for s in samples] == ["worker-a"]
        assert h.engine.registry.snapshot().by_id("worker-a").observed is not None


# --------------------------------------------------------------------------- #
# real agents: process pools behind the same HTTP surface
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="LocalWorkerAgent needs forkserver",
)
def test_real_local_worker_agents_complete_a_prime_count_over_http(tmp_path):
    with Harness(tmp_path, ("worker-a", "worker-b"), agents=False, slots=1) as h:
        job = h.engine.submit(job_type="prime_count", payload={"limit": 100_000})
        assert h.tick_until(
            lambda: h.job(job.job_id).status is JobStatus.COMPLETED, timeout_s=60.0
        )
        done = h.job(job.job_id)
        assert done.result["primes"] == 9592  # primes below 100_000
        assert done.attempts == 1
        assert done.assigned_worker in {"worker-a", "worker-b"}
        assert h.engine.registry.reserved_executions(done.assigned_worker) == ()
        # `delivered` is bumped after the POST returns, so it can trail the engine.
        assert wait_until(lambda: h.reporters[done.assigned_worker].delivered == 1)
        assert h.engine._stats["stale_results"] == 0
