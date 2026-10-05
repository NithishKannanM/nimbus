"""POST /internal/results, the control-plane endpoint remote workers call.

Two layers. The first mounts the router on a bare FastAPI app over a recording
fake engine, so what is under test is the wire contract alone: status code,
body, and exactly what the engine is handed. The second points the same router
at a real, unstarted local NimbusEngine and drains its completion queue by
hand, which is where the idempotency guards of docs/architecture.md section 8
become observable: exactly-once completion, stale-result accounting, and
tolerance of a result for a job that no longer exists.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.api import internal
from backend.api.deps import get_engine
from backend.config import NimbusConfig
from backend.models.enums import JobStatus, WorkloadClass
from backend.models.job import Job, utcnow
from backend.services.engine import NimbusEngine
from worker.executor import ExecutionResult


def make_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(internal.router)
    app.dependency_overrides[get_engine] = lambda: engine
    return app


def body(**overrides) -> dict:
    payload = {
        "job_id": "job-1",
        "execution_id": "job-1#1",
        "worker_id": "worker-1",
        "ok": True,
        "result": {"count": 168},
        "execution_time_ms": 12.5,
        "cpu_units": 1024,
        "memory_mb": 256,
    }
    payload.update(overrides)
    return payload


def running_job(*, job_id: str = "job-1", attempts: int = 1) -> Job:
    now = utcnow()
    return Job(
        job_id=job_id,
        job_type="prime_count",
        workload_class=WorkloadClass.CPU_BOUND,
        status=JobStatus.RUNNING,
        assigned_worker="worker-1",
        attempts=attempts,
        created_at=now - timedelta(seconds=2),
        scheduled_at=now - timedelta(seconds=1),
        started_at=now - timedelta(seconds=1),
    )


# --------------------------------------------------------------------------- #
# wire contract, against a recording fake engine
# --------------------------------------------------------------------------- #
class RecordingEngine:
    def __init__(self) -> None:
        self.reported: list[ExecutionResult] = []

    def report_result(self, result: ExecutionResult) -> None:
        self.reported.append(result)


@pytest.fixture
def fake() -> RecordingEngine:
    return RecordingEngine()


def test_post_result_is_202_and_echoes_the_execution_id(fake):
    resp = TestClient(make_app(fake)).post("/internal/results", json=body())
    assert resp.status_code == 202
    assert resp.json() == {"received": True, "execution_id": "job-1#1"}


def test_engine_receives_an_execution_result_equal_to_the_request(fake):
    TestClient(make_app(fake)).post("/internal/results", json=body())
    assert fake.reported == [
        ExecutionResult(
            job_id="job-1", execution_id="job-1#1", worker_id="worker-1", ok=True,
            result={"count": 168}, error=None, execution_time_ms=12.5,
            cpu_units=1024, memory_mb=256,
        )
    ]


def test_failure_report_carries_its_error_through(fake):
    resp = TestClient(make_app(fake)).post(
        "/internal/results", json=body(ok=False, result=None, error="boom")
    )
    assert resp.status_code == 202
    assert fake.reported[0].ok is False
    assert fake.reported[0].error == "boom"
    assert fake.reported[0].result is None


@pytest.mark.parametrize("missing", ["job_id", "execution_id", "worker_id", "ok"])
def test_missing_required_field_is_422_and_never_reaches_the_engine(fake, missing):
    payload = body()
    del payload[missing]
    resp = TestClient(make_app(fake)).post("/internal/results", json=payload)
    assert resp.status_code == 422
    assert fake.reported == []


def test_optional_fields_default_when_omitted(fake):
    minimal = {"job_id": "job-1", "execution_id": "job-1#1", "worker_id": "worker-1",
               "ok": True}
    resp = TestClient(make_app(fake)).post("/internal/results", json=minimal)
    assert resp.status_code == 202
    got = fake.reported[0]
    assert got.execution_time_ms == 0.0
    assert (got.result, got.error, got.cpu_units, got.memory_mb) == (None, None, 0, 0)


# --------------------------------------------------------------------------- #
# against a real, unstarted engine
# --------------------------------------------------------------------------- #
@pytest.fixture
def engine(tmp_path):
    # Never started: no scheduler thread, so the test decides when the
    # completion queue is drained and nothing races it.
    eng = NimbusEngine(NimbusConfig(backend="local", artifact_dir=str(tmp_path)))
    yield eng
    eng.stop(wait=False)


def test_result_for_a_running_job_completes_it_after_a_drain(engine):
    engine.store.put_job(running_job())
    resp = TestClient(make_app(engine)).post("/internal/results", json=body())
    assert resp.status_code == 202

    # Accepted is not applied: the write happens on the engine's own drain.
    assert engine.store.get_job("job-1").status is JobStatus.RUNNING
    engine._drain_completions()

    done = engine.store.get_job("job-1")
    assert done.status is JobStatus.COMPLETED
    assert done.result == {"count": 168}
    assert done.execution_time_ms == 12.5
    assert engine.stats()["completed"] == 1


def test_duplicate_result_is_counted_stale_and_completes_once(engine):
    engine.store.put_job(running_job())
    client = TestClient(make_app(engine))
    client.post("/internal/results", json=body())
    engine._drain_completions()
    resp = client.post("/internal/results", json=body())
    engine._drain_completions()

    assert resp.status_code == 202
    stats = engine.stats()
    assert stats["stale_results"] == 1
    assert stats["completed"] == 1
    assert engine.store.get_job("job-1").status is JobStatus.COMPLETED


def test_result_from_a_superseded_attempt_is_discarded(engine):
    # The job is on attempt 2; the slow first attempt reports late.
    engine.store.put_job(running_job(attempts=2))
    resp = TestClient(make_app(engine)).post(
        "/internal/results", json=body(execution_id="job-1#1")
    )
    engine._drain_completions()

    assert resp.status_code == 202
    job = engine.store.get_job("job-1")
    assert job.status is JobStatus.RUNNING
    assert job.result is None
    stats = engine.stats()
    assert stats["stale_results"] == 1
    assert stats["completed"] == 0


def test_result_for_the_current_attempt_still_lands_after_a_retry(engine):
    engine.store.put_job(running_job(attempts=2))
    TestClient(make_app(engine)).post("/internal/results", json=body(execution_id="job-1#2"))
    engine._drain_completions()
    assert engine.store.get_job("job-1").status is JobStatus.COMPLETED
    assert engine.stats()["stale_results"] == 0


def test_result_for_an_unknown_job_is_accepted_without_error(engine):
    resp = TestClient(make_app(engine)).post(
        "/internal/results", json=body(job_id="ghost", execution_id="ghost#1")
    )
    engine._drain_completions()

    assert resp.status_code == 202
    stats = engine.stats()
    assert stats["completed"] == 0
    assert stats["stale_results"] == 0


# --------------------------------------------------------------------------- #
# mounting under create_app
# --------------------------------------------------------------------------- #
def mounted_paths(config: NimbusConfig) -> set[str]:
    import backend.config as config_module  # noqa: PLC0415
    from backend.api.app import create_app  # noqa: PLC0415

    previous = config_module._config
    config_module.set_config(config)
    try:
        # The OpenAPI schema rather than app.routes: newer FastAPI nests
        # included routers, so their paths are not on the top-level list. The
        # lifespan is never entered, so no engine is built for either config.
        return set(create_app().openapi()["paths"])
    finally:
        config_module._config = previous


def test_internal_results_is_mounted_under_the_aws_backend(tmp_path):
    cfg = NimbusConfig(backend="aws", aws_region="us-east-1", artifact_dir=str(tmp_path))
    assert "/internal/results" in mounted_paths(cfg)


def test_internal_results_is_absent_for_local_inprocess(tmp_path):
    cfg = NimbusConfig(backend="local", artifact_dir=str(tmp_path))
    assert "/internal/results" not in mounted_paths(cfg)
