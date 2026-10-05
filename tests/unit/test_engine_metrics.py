"""Engine -> Channel 1 wiring.

The engine is never started: a FakeAgent stands in for the fleet and the tests
drive ``tick()`` and ``_handle_completion`` directly, so every emission is
observed at the exact call site rather than raced against a background loop.
"""

from __future__ import annotations

import time

from backend.config import NimbusConfig
from backend.models.enums import JobStatus, WorkerStatus
from backend.models.job import Job
from backend.services.engine import NimbusEngine
from backend.services.factory import build_metrics
from backend.services.metrics import (
    CloudWatchMetricsSink,
    InMemoryMetricsSink,
    Metric,
    NullMetricsSink,
)
from tests.unit.fakes_aws import FakeCloudWatch
from worker.executor import ExecutionResult

WORKER = "worker-1"


class FakeAgent:
    def __init__(self) -> None:
        self.slots = 4
        self.cores = ()
        self.active_jobs = 0
        self.received: list[Job] = []

    def start(self) -> None: ...
    def stop(self, *, wait: bool = True) -> None: ...

    def try_execute(self, job: Job) -> bool:
        self.received.append(job)
        return True

    def abandon(self, execution_id: str) -> bool:
        return True

    def child_pids(self) -> tuple[int, ...]:
        return ()

    def alive(self) -> bool:
        return True

    def stats(self) -> dict:
        return {"duplicate_dispatches": 0, "abandoned_executions": 0, "crashes": 0}


def make_engine(tmp_path, **overrides) -> NimbusEngine:
    cfg = dict(
        backend="local",
        artifact_dir=str(tmp_path / "artifacts"),
        strategy="fifo",
        metrics_sample_interval_s=0.0,
        retry_base_s=0.001,
        retry_cap_s=0.001,
        retry_jitter_ratio=0.0,
    )
    cfg.update(overrides)
    engine = NimbusEngine(NimbusConfig(**cfg))
    engine._workers[WORKER] = FakeAgent()
    engine.registry.register(
        WORKER, slots=4, capacity_cpu_units=4096, capacity_memory_mb=8192
    )
    engine.registry.heartbeat(WORKER)
    return engine


def running_job(engine: NimbusEngine) -> Job:
    job = engine.submit(job_type="prime_count", payload={"limit": 1000})
    engine.tick()
    stored = engine.store.get_job(job.job_id)
    assert stored.status is JobStatus.RUNNING
    return stored


def result_for(job: Job, *, ok: bool = True, ms: float = 12.5) -> ExecutionResult:
    return ExecutionResult(
        job_id=job.job_id, execution_id=job.execution_id, worker_id=WORKER, ok=ok,
        result={"x": 1} if ok else None, error=None if ok else "boom",
        execution_time_ms=ms, cpu_units=job.cpu_units, memory_mb=job.memory_mb,
    )


def test_local_engine_uses_the_in_memory_sink(tmp_path):
    assert isinstance(make_engine(tmp_path).metrics, InMemoryMetricsSink)


def test_metrics_disabled_uses_the_null_sink(tmp_path):
    engine = make_engine(tmp_path, metrics_enabled=False)
    assert isinstance(engine.metrics, NullMetricsSink)
    running_job(engine)  # emission against the null sink must be harmless


def test_build_metrics_under_aws_is_a_cloudwatch_sink(tmp_path, monkeypatch):
    import backend.aws.clients as clients

    fake = FakeCloudWatch()
    monkeypatch.setattr(clients, "client", lambda *a, **k: fake)
    sink = build_metrics(NimbusConfig(backend="aws", metrics_flush_interval_s=5.0))
    assert isinstance(sink, CloudWatchMetricsSink)
    assert isinstance(build_metrics(NimbusConfig(backend="aws", metrics_enabled=False)),
                      NullMetricsSink)


def test_submit_emits_jobs_submitted(tmp_path):
    engine = make_engine(tmp_path)
    engine.submit(job_type="prime_count", payload={"limit": 1000})
    assert engine.metrics.total(Metric.JOBS_SUBMITTED, WorkloadClass="CPU_BOUND") == 1


def test_dispatch_emits_latency_per_decision_and_queue_wait_once(tmp_path):
    engine = make_engine(tmp_path)
    running_job(engine)
    assert engine.metrics.count(Metric.SCHEDULING_LATENCY_MS, Strategy="fifo") == 1
    assert engine.metrics.count(Metric.QUEUE_WAIT_MS, Strategy="fifo",
                                WorkloadClass="CPU_BOUND") == 1


def test_a_retry_attempt_does_not_emit_queue_wait_again(tmp_path):
    engine = make_engine(tmp_path)
    job = running_job(engine)
    engine._handle_completion(result_for(job, ok=False))
    time.sleep(0.05)  # backoff
    engine.tick()
    assert engine.store.get_job(job.job_id).attempts == 2
    assert engine.metrics.count(Metric.QUEUE_WAIT_MS) == 1
    assert engine.metrics.count(Metric.SCHEDULING_LATENCY_MS) == 2


def test_completion_emits_completed_and_execution_time(tmp_path):
    engine = make_engine(tmp_path)
    job = running_job(engine)
    engine._handle_completion(result_for(job, ms=42.0))
    dims = dict(Strategy="fifo", WorkloadClass="CPU_BOUND")
    assert engine.metrics.total(Metric.JOBS_COMPLETED, **dims) == 1
    assert engine.metrics.total(Metric.EXECUTION_TIME_MS, **dims) == 42.0


def test_a_replayed_result_is_a_duplicate_not_a_second_completion(tmp_path):
    engine = make_engine(tmp_path)
    job = running_job(engine)
    engine._handle_completion(result_for(job))
    engine._handle_completion(result_for(job))
    assert engine.metrics.total(Metric.JOBS_COMPLETED) == 1
    assert engine.metrics.total(Metric.DUPLICATE_EXECUTIONS, Strategy="fifo") == 1


def test_failure_with_budget_left_emits_retried(tmp_path):
    engine = make_engine(tmp_path, max_attempts=3)
    job = running_job(engine)
    engine._handle_completion(result_for(job, ok=False))
    dims = dict(Strategy="fifo", WorkloadClass="CPU_BOUND")
    assert engine.metrics.total(Metric.JOBS_RETRIED, **dims) == 1
    assert engine.metrics.total(Metric.JOBS_FAILED) == 0


def test_failure_with_no_budget_emits_failed(tmp_path):
    engine = make_engine(tmp_path, max_attempts=1)
    job = running_job(engine)
    engine._handle_completion(result_for(job, ok=False))
    dims = dict(Strategy="fifo", WorkloadClass="CPU_BOUND")
    assert engine.metrics.total(Metric.JOBS_FAILED, **dims) == 1
    assert engine.metrics.total(Metric.JOBS_RETRIED) == 0


def test_gauge_sampling_reports_depth_and_per_worker_health(tmp_path):
    engine = make_engine(tmp_path)
    engine.submit(job_type="prime_count", payload={"limit": 1000})
    engine._maybe_sample_gauges()
    assert engine.metrics.total(Metric.QUEUE_DEPTH_VISIBLE) == 1
    assert engine.metrics.total(Metric.QUEUE_DEPTH_IN_FLIGHT) == 0
    assert engine.metrics.total(Metric.WORKER_HEALTHY, WorkerId=WORKER) == 1

    # Health is derived from heartbeat age, so UNHEALTHY cannot be set directly;
    # DRAINING is the administrative not-healthy state.
    engine.registry.set_status(WORKER, WorkerStatus.DRAINING)
    engine._maybe_sample_gauges()
    assert engine.metrics.count(Metric.WORKER_HEALTHY, WorkerId=WORKER) == 2
    assert engine.metrics.total(Metric.WORKER_HEALTHY, WorkerId=WORKER) == 1  # 1 + 0


def test_gauges_respect_the_sample_interval(tmp_path):
    engine = make_engine(tmp_path, metrics_sample_interval_s=3600.0)
    engine._last_gauge_sample = time.monotonic()
    engine.tick()
    assert engine.metrics.count(Metric.QUEUE_DEPTH_VISIBLE) == 0
    engine._metrics_sample_interval_s = 0.0
    engine.tick()
    assert engine.metrics.count(Metric.QUEUE_DEPTH_VISIBLE) == 1


def test_stop_flushes_the_sink(tmp_path):
    engine = make_engine(tmp_path)
    engine.submit(job_type="prime_count", payload={"limit": 1000})
    assert engine.metrics._pending.snapshot()  # noqa: SLF001
    engine.stop(wait=False)
    assert not engine.metrics._pending.snapshot()  # noqa: SLF001
    assert engine.metrics.total(Metric.JOBS_SUBMITTED) == 1  # now in `published`


def test_stats_and_reliability_keys_are_unchanged(tmp_path):
    engine = make_engine(tmp_path)
    assert "metrics" not in engine.stats()
    assert "metrics" not in engine.reliability()
