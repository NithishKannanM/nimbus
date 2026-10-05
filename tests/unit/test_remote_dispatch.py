"""The HTTP data plane, control-plane side.

Everything here runs against ``httpx.MockTransport``: no sockets, no worker
process. What is under test is the contract between the engine and a remote
worker -- accept, reject, fail, mis-identify -- and that each outcome lands on
the right engine path (docs/architecture.md section 3.3 and section 8).
"""

from __future__ import annotations

import json
import multiprocessing

import httpx
import pytest

from backend.config import NimbusConfig
from backend.models.enums import JobStatus, WorkloadClass
from backend.models.job import Job
from backend.services.engine import NimbusEngine
from backend.services.factory import (
    build_telemetry_collector,
    build_workers,
    validate,
)
from backend.services.registry import WorkerRegistry
from backend.services.remote import (
    RemoteTelemetryCollector,
    RemoteWorkerClient,
    parse_worker_endpoints,
)
from worker.agent import LocalWorkerAgent
from worker.telemetry import TelemetryCollector


def make_job() -> Job:
    return Job(job_type="prime_count", workload_class=WorkloadClass.CPU_BOUND,
               payload={"limit": 1000})


def client_for(handler, *, worker_id: str = "worker-1", reservations=lambda _w: 0,
               **kwargs) -> RemoteWorkerClient:
    client = RemoteWorkerClient(
        worker_id,
        "http://worker-1.test:8080",
        slots=2,
        reservations=reservations,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )
    client.start()
    return client


def accepting(worker_id: str = "worker-1"):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/execute"
        body = json.loads(request.content)
        return httpx.Response(
            202,
            json={"accepted": True, "worker_id": worker_id,
                  "execution_id": f"{body['job_id']}#{body['attempts']}",
                  "active_jobs": 1},
        )

    return handler


# --------------------------------------------------------------------------- #
# endpoint parsing
# --------------------------------------------------------------------------- #
def test_parse_worker_endpoints_preserves_order_and_trims_slashes():
    parsed = parse_worker_endpoints(
        "worker-2=http://10.0.0.2:8080/, worker-1=https://10.0.0.1:8080"
    )
    assert list(parsed) == ["worker-2", "worker-1"]
    assert parsed == {"worker-2": "http://10.0.0.2:8080", "worker-1": "https://10.0.0.1:8080"}


@pytest.mark.parametrize(
    "spec",
    [
        "",
        "   ",
        "worker-1=http://a:1,worker-1=http://b:1",  # duplicate id
        "worker-1=",  # empty url
        "=http://a:1",  # empty id
        "worker-1=ftp://a:1",  # bad scheme
        "worker-1=a:1",  # no scheme
        "worker-1",  # no separator
        "worker-1=http://a:1,,",  # empty entry
    ],
)
def test_parse_worker_endpoints_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        parse_worker_endpoints(spec)


# --------------------------------------------------------------------------- #
# try_execute
# --------------------------------------------------------------------------- #
def test_a_202_is_accepted_and_the_job_round_trips():
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return accepting()(request)

    client = client_for(handler)
    job = make_job()
    assert client.try_execute(job) is True
    assert client.dispatched == 1
    assert seen[0].method == "POST" and seen[0].url.path == "/execute"
    echoed = Job.model_validate(json.loads(seen[0].content))
    assert echoed.execution_id == job.execution_id
    assert echoed.job_id == job.job_id


def test_a_409_is_a_rejection_not_an_error():
    client = client_for(lambda r: httpx.Response(409, json={"detail": "at capacity"}))
    assert client.try_execute(make_job()) is False
    assert client.rejected == 1
    assert client.dispatch_errors == 0


def test_a_500_is_a_dispatch_error():
    client = client_for(lambda r: httpx.Response(500))
    assert client.try_execute(make_job()) is False
    assert client.dispatch_errors == 1
    assert client.rejected == 0


def test_an_unreachable_worker_is_a_dispatch_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = client_for(handler)
    assert client.try_execute(make_job()) is False
    assert client.dispatch_errors == 1


def test_a_read_timeout_is_a_dispatch_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    client = client_for(handler)
    assert client.try_execute(make_job()) is False
    assert client.dispatch_errors == 1


def test_a_202_from_the_wrong_worker_is_not_accepted():
    client = client_for(accepting(worker_id="worker-9"))
    assert client.try_execute(make_job()) is False
    assert client.identity_mismatches == 1
    assert client.dispatched == 0


def test_a_202_with_a_garbage_body_is_not_accepted():
    client = client_for(lambda r: httpx.Response(202, content=b"not json"))
    assert client.try_execute(make_job()) is False
    assert client.identity_mismatches == 1


# --------------------------------------------------------------------------- #
# liveness and the rest of the agent surface
# --------------------------------------------------------------------------- #
def health(**body):
    payload = {"ok": True, "worker_id": "worker-1", "crashed": False, **body}
    return lambda request: httpx.Response(200, json=payload)


def test_alive_true_for_a_healthy_worker():
    client = client_for(health())
    assert client.alive() is True
    assert client.crashed is False


def test_alive_false_when_the_worker_reports_a_crash():
    client = client_for(health(crashed=True))
    assert client.alive() is False
    assert client.crashed is True


def test_alive_false_when_not_ok():
    assert client_for(health(ok=False)).alive() is False


def test_alive_false_on_http_error_and_counts_a_probe_failure():
    client = client_for(lambda r: httpx.Response(500))
    assert client.alive() is False
    assert client.probe_failures == 1


def test_alive_false_when_unreachable():
    def handler(request):
        raise httpx.ConnectTimeout("nope", request=request)

    client = client_for(handler)
    assert client.alive() is False
    assert client.probe_failures == 1


def test_alive_false_for_the_wrong_worker_id():
    client = client_for(health(worker_id="worker-2"))
    assert client.alive() is False
    assert client.identity_mismatches == 1


def test_abandon_posts_the_execution_id():
    posted: list[dict] = []

    def handler(request):
        assert request.url.path == "/abandon"
        posted.append(json.loads(request.content))
        return httpx.Response(200, json={"abandoned": True})

    client = client_for(handler)
    assert client.abandon("exec-1") is True
    client.stop(wait=True)  # drains the abandon queue
    assert posted == [{"execution_id": "exec-1"}]


def test_abandon_does_not_raise_when_the_worker_is_unreachable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    client = client_for(handler)
    assert client.abandon("exec-1") is True
    client.stop(wait=True)


def test_active_jobs_reads_the_reservation_callable():
    counts = {"worker-1": 0}
    client = client_for(health(), reservations=lambda wid: counts[wid])
    assert client.active_jobs == 0
    counts["worker-1"] = 2
    assert client.active_jobs == 2


def test_hard_failure_cannot_be_simulated_remotely():
    client = client_for(health())
    with pytest.raises(NotImplementedError):
        client.crash()
    assert client.recover() is None
    assert client.child_pids() == ()
    assert client.cores is None


def test_stats_merges_the_remote_view_and_falls_back_to_zeros():
    reachable = client_for(
        lambda r: httpx.Response(200, json={"active_jobs": 1, "slots": 2, "crashed": False,
                                            "duplicate_dispatches": 3,
                                            "abandoned_executions": 0, "crashes": 0})
    )
    assert reachable.stats()["duplicate_dispatches"] == 3
    assert reachable.stats()["reachable"] is True

    down = client_for(lambda r: httpx.Response(503))
    stats = down.stats()
    assert stats["reachable"] is False
    # Same keys the engine's reliability() sums over.
    for key in ("duplicate_dispatches", "abandoned_executions", "crashes"):
        assert stats[key] == 0


# --------------------------------------------------------------------------- #
# telemetry collector
# --------------------------------------------------------------------------- #
def telemetry_body(worker_id: str, cpu: float = 12.5) -> dict:
    return {"worker_id": worker_id, "cpu_percent": cpu, "memory_percent": 30.0,
            "available_memory_mb": 1000.0, "active_jobs": 0}


def fleet_of(handlers: dict[str, object]) -> dict[str, RemoteWorkerClient]:
    clients = {}
    for wid, handler in handlers.items():
        client = RemoteWorkerClient(
            wid, f"http://{wid}.test:8080", slots=2, reservations=lambda _w: 0,
            transport=httpx.MockTransport(handler),
        )
        client.start()
        clients[wid] = client
    return clients


def registry_with(*worker_ids: str, clock=None) -> WorkerRegistry:
    kwargs = {"clock": clock} if clock else {}
    registry = WorkerRegistry(heartbeat_timeout_s=5.0, eviction_timeout_s=60.0,
                              telemetry_history=10, telemetry_window=3, **kwargs)
    for wid in worker_ids:
        registry.register(wid, slots=2, capacity_cpu_units=2048, capacity_memory_mb=4096)
    return registry


def test_collect_once_ingests_every_reachable_worker():
    registry = registry_with("worker-1", "worker-2")
    clients = fleet_of({
        wid: (lambda request, wid=wid: httpx.Response(200, json=telemetry_body(wid)))
        for wid in ("worker-1", "worker-2")
    })
    collector = RemoteTelemetryCollector(
        registry, workers=lambda: clients, interval_s=1.0, is_alive=lambda _w: True
    )
    samples = collector.collect_once()
    assert sorted(s.worker_id for s in samples) == ["worker-1", "worker-2"]
    assert registry.telemetry_history("worker-1")[-1].cpu_percent == 12.5
    assert collector.stats.samples == 2 and collector.stats.ticks == 1
    assert collector.stats.errors == 0


def test_collect_once_skips_workers_simulated_as_failed():
    """ingest_telemetry refreshes the heartbeat, so polling a partitioned
    worker would silently undo the partition."""
    now = [100.0]
    registry = registry_with("worker-1", "worker-2", clock=lambda: now[0])
    clients = fleet_of({
        wid: (lambda request, wid=wid: httpx.Response(200, json=telemetry_body(wid)))
        for wid in ("worker-1", "worker-2")
    })
    collector = RemoteTelemetryCollector(
        registry, workers=lambda: clients, interval_s=1.0,
        is_alive=lambda wid: wid != "worker-2",
    )
    now[0] = 110.0
    samples = collector.collect_once()
    assert [s.worker_id for s in samples] == ["worker-1"]
    assert registry.telemetry_history("worker-2") == []
    ages = {w.worker_id: w.heartbeat_age_s for w in registry.snapshot().workers}
    assert ages["worker-1"] == pytest.approx(0.0)
    assert ages["worker-2"] == pytest.approx(10.0)


def test_a_failing_worker_counts_an_error_and_the_rest_still_ingest():
    registry = registry_with("worker-1", "worker-2")
    clients = fleet_of({
        "worker-1": lambda request: httpx.Response(500),
        "worker-2": lambda request: httpx.Response(200, json=telemetry_body("worker-2")),
    })
    collector = RemoteTelemetryCollector(
        registry, workers=lambda: clients, interval_s=1.0, is_alive=lambda _w: True
    )
    samples = collector.collect_once()
    assert [s.worker_id for s in samples] == ["worker-2"]
    assert collector.stats.errors == 1
    assert registry.telemetry_history("worker-1") == []


def test_telemetry_from_the_wrong_worker_is_not_ingested():
    registry = registry_with("worker-1")
    clients = fleet_of({"worker-1": lambda r: httpx.Response(200, json=telemetry_body("worker-7"))})
    collector = RemoteTelemetryCollector(
        registry, workers=lambda: clients, interval_s=1.0, is_alive=lambda _w: True
    )
    assert collector.collect_once() == []
    assert clients["worker-1"].identity_mismatches == 1


def test_the_collector_thread_starts_and_stops():
    collector = RemoteTelemetryCollector(
        registry_with(), workers=lambda: {}, interval_s=0.01, is_alive=lambda _w: True
    )
    collector.start()
    assert collector.running
    collector.stop()
    assert not collector.running


# --------------------------------------------------------------------------- #
# factory
# --------------------------------------------------------------------------- #
def http_config(tmp_path, **overrides) -> NimbusConfig:
    defaults = dict(
        backend="local",
        worker_transport="http",
        worker_endpoints="worker-a=http://a.test:8080,worker-b=http://b.test:8080",
        worker_slots=2,
        artifact_dir=str(tmp_path / "artifacts"),
        scheduler_tick_s=0.01,
        max_attempts=3,
    )
    defaults.update(overrides)
    return NimbusConfig(**defaults)


def test_build_workers_inprocess_returns_unstarted_local_agents(tmp_path):
    cfg = NimbusConfig(backend="local", worker_count=3, worker_slots=2,
                       artifact_dir=str(tmp_path))
    workers = build_workers(cfg, sink=lambda _r: None, cores_for=lambda _i: None,
                            reservations=lambda _w: 0)
    assert list(workers) == ["worker-1", "worker-2", "worker-3"]
    assert all(isinstance(w, LocalWorkerAgent) for w in workers.values())
    assert all(w.slots == 2 and w._pool is None for w in workers.values())


def test_build_workers_http_returns_remote_clients_keyed_by_endpoint_id(tmp_path):
    workers = build_workers(http_config(tmp_path), sink=lambda _r: None,
                            cores_for=lambda _i: None, reservations=lambda _w: 0)
    assert list(workers) == ["worker-a", "worker-b"]
    assert all(isinstance(w, RemoteWorkerClient) for w in workers.values())
    assert all(w.slots == 2 for w in workers.values())


def test_build_telemetry_collector_matches_the_transport(tmp_path):
    kwargs = dict(workers=lambda: {}, pid_provider=lambda _w: (),
                  active_jobs_provider=lambda _w: 0, is_alive=lambda _w: True)
    local = build_telemetry_collector(
        NimbusConfig(backend="local", artifact_dir=str(tmp_path)), registry_with(), **kwargs
    )
    remote = build_telemetry_collector(http_config(tmp_path), registry_with(), **kwargs)
    assert isinstance(local, TelemetryCollector)
    assert isinstance(remote, RemoteTelemetryCollector)


def test_validate_reports_transport_problems(tmp_path):
    assert validate(NimbusConfig(backend="local", artifact_dir=str(tmp_path))) == []
    assert validate(http_config(tmp_path)) == []
    assert any("transport" in p for p in validate(http_config(tmp_path, worker_transport="carrier-pigeon")))
    assert any("WORKER_ENDPOINTS" in p for p in validate(http_config(tmp_path, worker_endpoints="")))
    bad = validate(http_config(tmp_path, worker_endpoints="worker-1=ftp://x:1"))
    assert any("invalid" in p for p in bad)


# --------------------------------------------------------------------------- #
# engine over HTTP
# --------------------------------------------------------------------------- #
class Recorder:
    """Per-worker mock transport that records what the engine sent."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.executed: list[Job] = []

    def __call__(self, wid: str) -> httpx.BaseTransport:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/execute":
                job = Job.model_validate(json.loads(request.content))
                self.executed.append(job)
                if self.status == 409:
                    return httpx.Response(409, json={"detail": "at capacity"})
                return httpx.Response(202, json={"accepted": True, "worker_id": wid,
                                                 "execution_id": job.execution_id,
                                                 "active_jobs": 1})
            if request.url.path == "/health":
                return httpx.Response(200, json={"ok": True, "worker_id": wid,
                                                 "crashed": False})
            return httpx.Response(404)

        return httpx.MockTransport(handler)


def test_engine_http_fleet_is_empty_until_started_then_matches_endpoints(tmp_path):
    engine = NimbusEngine(http_config(tmp_path), http_transport=Recorder())
    assert engine._workers == {}
    engine._start_fleet()
    assert list(engine._workers) == ["worker-a", "worker-b"]
    assert engine.registry.worker_ids() == ["worker-a", "worker-b"]
    assert isinstance(engine.telemetry, RemoteTelemetryCollector)
    engine.stop(wait=False)


def test_engine_dispatches_over_http_and_reaches_running(tmp_path):
    recorder = Recorder()
    engine = NimbusEngine(http_config(tmp_path), http_transport=recorder)
    engine._start_fleet()
    job = engine.submit(job_type="prime_count", payload={"limit": 1000})
    engine.tick()
    stored = engine.store.get_job(job.job_id)
    assert stored.status is JobStatus.RUNNING
    assert stored.attempts == 1
    assert len(recorder.executed) == 1
    assert recorder.executed[0].execution_id == stored.execution_id
    # The reservation is what a remote client reports as active_jobs.
    assert engine._workers[stored.assigned_worker].active_jobs == 1
    engine.stop(wait=False)


def test_engine_409_costs_no_attempt(tmp_path):
    recorder = Recorder(status=409)
    engine = NimbusEngine(http_config(tmp_path), http_transport=recorder)
    engine._start_fleet()
    job = engine.submit(job_type="prime_count", payload={"limit": 1000})
    engine.tick()
    stored = engine.store.get_job(job.job_id)
    assert engine._stats["dispatch_rejected"] == 1
    assert stored.attempts == 0
    assert stored.status is JobStatus.QUEUED
    assert len(recorder.executed) == 1
    engine.stop(wait=False)


def test_engine_probes_remote_health_for_heartbeats(tmp_path):
    def transport_for(wid):
        status = 500 if wid == "worker-b" else 200
        return httpx.MockTransport(
            lambda r: httpx.Response(status, json={"ok": True, "worker_id": wid,
                                                   "crashed": False})
        )

    engine = NimbusEngine(http_config(tmp_path), http_transport=transport_for)
    engine._start_fleet()
    assert engine._worker_alive("worker-a") is True
    assert engine._worker_alive("worker-b") is False
    assert engine._worker_alive("unknown") is True
    assert engine.heartbeats.beat_once() == 1
    engine.stop(wait=False)


def test_engine_hard_failure_is_unsupported_for_remote_but_partition_works(tmp_path):
    engine = NimbusEngine(http_config(tmp_path), http_transport=Recorder())
    engine._start_fleet()
    with pytest.raises(NotImplementedError):
        engine.fail_worker("worker-a", hard=True)
    result = engine.fail_worker("worker-b", hard=False)
    assert result["mode"] == "partition"
    assert "worker-b" in engine._failed_workers
    engine.stop(wait=False)


def test_engine_rejects_an_invalid_http_config(tmp_path):
    with pytest.raises(ValueError):
        NimbusEngine(http_config(tmp_path, worker_endpoints=""))


@pytest.mark.skipif(
    "forkserver" not in multiprocessing.get_all_start_methods(),
    reason="LocalWorkerAgent needs forkserver",
)
def test_engine_inprocess_fleet_is_unchanged(tmp_path):
    cfg = NimbusConfig(backend="local", worker_count=2, worker_slots=1,
                       artifact_dir=str(tmp_path / "artifacts"))
    engine = NimbusEngine(cfg)
    try:
        engine._start_fleet()
        assert list(engine._workers) == ["worker-1", "worker-2"]
        assert all(isinstance(w, LocalWorkerAgent) for w in engine._workers.values())
        assert engine._worker_alive("worker-1") is True
    finally:
        engine.stop(wait=False)


# --------------------------------------------------------------------------- #
# internal router mounting
# --------------------------------------------------------------------------- #
def mounted_paths(config: NimbusConfig) -> set[str]:
    import backend.config as config_module  # noqa: PLC0415
    from backend.api.app import create_app  # noqa: PLC0415

    previous = config_module._config
    config_module.set_config(config)
    try:
        # The OpenAPI schema rather than app.routes: newer FastAPI nests
        # included routers, so their paths are not on the top-level list.
        return set(create_app().openapi()["paths"])
    finally:
        config_module._config = previous


def test_internal_results_mounted_for_http_transport_on_the_local_backend(tmp_path):
    assert "/internal/results" in mounted_paths(http_config(tmp_path))


def test_internal_results_not_mounted_for_local_inprocess(tmp_path):
    cfg = NimbusConfig(backend="local", artifact_dir=str(tmp_path))
    assert "/internal/results" not in mounted_paths(cfg)
