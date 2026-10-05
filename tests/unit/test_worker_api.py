"""The HTTP worker surface (worker/api.py), with a fake agent and mock transports.

No process pool and no network: the agent is injected, and the result reporter
talks to ``httpx.MockTransport``. The one real-agent test is Linux-only because
LocalWorkerAgent needs the forkserver start method.
"""

from __future__ import annotations

import json
import multiprocessing
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from backend.api.schemas import ExecutionResultRequest
from backend.models.enums import WorkloadClass
from backend.models.job import Job
from backend.models.telemetry import WorkerTelemetry
from worker.api import ResultReporter, create_app, result_payload
from worker.executor import ExecutionResult

WORKER_ID = "worker-7"


def make_job(attempts: int = 1) -> Job:
    return Job(
        job_id="job-abc123",
        job_type="prime_count",
        workload_class=WorkloadClass.CPU_BOUND,
        payload={"limit": 1000},
        attempts=attempts,
    )


def make_result(**overrides) -> ExecutionResult:
    fields = dict(
        job_id="job-abc123", execution_id="job-abc123#1", worker_id=WORKER_ID,
        ok=True, result={"primes": 168}, error=None, execution_time_ms=12.5,
        cpu_units=512, memory_mb=256,
    )
    fields.update(overrides)
    return ExecutionResult(**fields)


class FakeAgent:
    def __init__(self, *, accept: bool = True, slots: int = 2) -> None:
        self.worker_id = WORKER_ID
        self.slots = slots
        self.accept = accept
        self.crashed = False
        self.active_jobs = 0
        self.received: list[Job] = []
        self.abandoned: list[str] = []
        self.abandon_result = True

    def start(self) -> None: ...
    def stop(self, *, wait: bool = True) -> None: ...

    def try_execute(self, job: Job) -> bool:
        self.received.append(job)
        return self.accept

    def abandon(self, execution_id: str) -> bool:
        self.abandoned.append(execution_id)
        return self.abandon_result

    def child_pids(self) -> tuple[int, ...]:
        return ()

    def stats(self) -> dict:
        return {"active_jobs": self.active_jobs, "slots": self.slots, "crashed": self.crashed}


@pytest.fixture
def agent() -> FakeAgent:
    return FakeAgent()


@pytest.fixture
def client(agent: FakeAgent) -> TestClient:
    # Not used as a context manager, so the lifespan never starts anything.
    app = create_app(worker_id=WORKER_ID, agent=agent)
    return TestClient(app)


# --------------------------------------------------------------------------- #
# endpoints
# --------------------------------------------------------------------------- #
def test_execute_accepts_with_202(client, agent):
    job = make_job()
    response = client.post("/execute", json=job.model_dump(mode="json"))
    assert response.status_code == 202
    body = response.json()
    assert body["accepted"] is True
    assert body["worker_id"] == WORKER_ID
    assert body["execution_id"] == job.execution_id


def test_execute_hands_the_agent_the_same_job(client, agent):
    job = make_job(attempts=2)
    client.post("/execute", json=job.model_dump(mode="json"))
    assert len(agent.received) == 1
    assert agent.received[0].execution_id == job.execution_id == "job-abc123#2"
    assert agent.received[0].payload == {"limit": 1000}


def test_execute_at_capacity_is_409(agent):
    agent.accept = False
    app = create_app(worker_id=WORKER_ID, agent=agent)
    response = TestClient(app).post("/execute", json=make_job().model_dump(mode="json"))
    assert response.status_code == 409
    assert "at capacity" in response.json()["detail"]


def test_execute_rejects_malformed_body(client, agent):
    response = client.post("/execute", json={"job_type": "prime_count"})
    assert response.status_code == 422
    assert agent.received == []


def test_abandon_passes_through(client, agent):
    response = client.post("/abandon", json={"execution_id": "job-abc123#1"})
    assert response.status_code == 200
    assert response.json() == {"abandoned": True}
    assert agent.abandoned == ["job-abc123#1"]

    agent.abandon_result = False
    assert client.post("/abandon", json={"execution_id": "x#1"}).json() == {"abandoned": False}


def test_abandon_requires_execution_id(client):
    assert client.post("/abandon", json={}).status_code == 422


def test_health_reports_crashed(client, agent):
    assert client.get("/health").json() == {"ok": True, "worker_id": WORKER_ID, "crashed": False}
    agent.crashed = True
    assert client.get("/health").json()["crashed"] is True


def test_telemetry_is_a_valid_sample_for_this_worker(client):
    response = client.get("/telemetry")
    assert response.status_code == 200
    sample = WorkerTelemetry.model_validate(response.json())
    assert sample.worker_id == WORKER_ID


def test_stats_include_delivery_counters(agent):
    reporter = ResultReporter("http://cp", worker_id=WORKER_ID)
    reporter.delivered, reporter.undelivered = 4, 1
    app = create_app(worker_id=WORKER_ID, agent=agent, reporter=reporter)
    body = TestClient(app).get("/stats").json()
    assert body["results_delivered"] == 4
    assert body["results_undelivered"] == 1
    assert body["slots"] == agent.slots


# --------------------------------------------------------------------------- #
# ResultReporter
# --------------------------------------------------------------------------- #
class Recorder:
    """A MockTransport handler that replays a scripted sequence of outcomes."""

    def __init__(self, *outcomes: int | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else 202
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome, json={})

    @property
    def calls(self) -> int:
        return len(self.requests)


def run_reporter(recorder: Recorder, *results: ExecutionResult, retries: int = 3) -> ResultReporter:
    reporter = ResultReporter(
        "http://cp:8000/", worker_id=WORKER_ID, retries=retries,
        transport=httpx.MockTransport(recorder),
    )
    reporter.start()
    for result in results:
        reporter.submit(result)
    reporter.stop()  # drains the queue, then joins
    return reporter


def test_result_payload_matches_the_wire_schema():
    payload = result_payload(make_result())
    assert set(payload) == set(ExecutionResultRequest.model_fields)
    ExecutionResultRequest.model_validate(payload)


def test_reporter_posts_the_result_to_internal_results():
    recorder = Recorder(202)
    reporter = run_reporter(recorder, make_result())
    assert reporter.delivered == 1 and reporter.undelivered == 0
    request = recorder.requests[0]
    assert request.method == "POST"
    assert str(request.url) == "http://cp:8000/internal/results"
    body = json.loads(request.content)
    assert set(body) == set(ExecutionResultRequest.model_fields)
    assert body["execution_id"] == "job-abc123#1"
    assert body["result"] == {"primes": 168}


def test_reporter_retries_server_errors_until_accepted():
    recorder = Recorder(500, 500, 202)
    reporter = run_reporter(recorder, make_result())
    assert recorder.calls == 3
    assert reporter.delivered == 1 and reporter.undelivered == 0


def test_reporter_gives_up_after_exactly_retries_attempts():
    recorder = Recorder(*([500] * 10))
    reporter = run_reporter(recorder, make_result(), retries=3)
    assert recorder.calls == 3
    assert reporter.delivered == 0 and reporter.undelivered == 1


def test_reporter_retries_connection_errors():
    recorder = Recorder(httpx.ConnectError("refused"), 202)
    reporter = run_reporter(recorder, make_result())
    assert recorder.calls == 2
    assert reporter.delivered == 1


def test_reporter_drains_everything_queued_before_stop():
    recorder = Recorder()
    reporter = run_reporter(
        recorder,
        make_result(execution_id="job-abc123#1"),
        make_result(execution_id="job-abc123#2"),
        make_result(execution_id="job-abc123#3"),
    )
    assert reporter.delivered == 3
    assert [json.loads(r.content)["execution_id"] for r in recorder.requests] == [
        "job-abc123#1", "job-abc123#2", "job-abc123#3",
    ]
    assert reporter._thread is None


def test_create_app_wires_the_injected_reporter_and_agent(agent):
    reporter = ResultReporter("http://cp", worker_id=WORKER_ID)
    app = create_app(worker_id=WORKER_ID, agent=agent, reporter=reporter)
    assert app.state.agent is agent
    assert app.state.reporter is reporter


# --------------------------------------------------------------------------- #
# real agent (needs forkserver)
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="LocalWorkerAgent needs the forkserver start method (not available on Windows)",
)
def test_real_agent_result_reaches_the_control_plane(tmp_path):
    from worker.agent import LocalWorkerAgent

    recorder = Recorder()
    reporter = ResultReporter(
        "http://cp", worker_id=WORKER_ID, transport=httpx.MockTransport(recorder)
    )
    real = LocalWorkerAgent(
        WORKER_ID, slots=1, scratch_dir=str(tmp_path), sink=reporter.submit
    )
    app = create_app(worker_id=WORKER_ID, agent=real, reporter=reporter)
    job = Job(
        job_type="prime_count", workload_class=WorkloadClass.CPU_BOUND,
        payload={"limit": 100000},
    )
    with TestClient(app) as client:  # runs the lifespan: starts agent and reporter
        assert client.post("/execute", json=job.model_dump(mode="json")).status_code == 202
        deadline = time.time() + 30
        while recorder.calls == 0 and time.time() < deadline:
            time.sleep(0.05)
    assert recorder.calls == 1
    body = json.loads(recorder.requests[0].content)
    assert body["ok"] is True
    assert body["result"]["primes"] == 9592
    assert body["execution_id"] == job.execution_id
