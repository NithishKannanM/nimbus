"""Telemetry collection and heartbeats.

Two mechanisms, deliberately independent: heartbeats prove liveness, telemetry
observes resources. With telemetry off, workers must still be schedulable.
"""

from __future__ import annotations

import time

import pytest

from backend.models.enums import WorkerStatus
from backend.models.telemetry import WorkerTelemetry
from backend.services.registry import WorkerRegistry
from worker.heartbeat import HeartbeatEmitter
from worker.telemetry import CollectorStats, TelemetryCollector, process_memory_mb


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(clock: FakeClock) -> WorkerRegistry:
    reg = WorkerRegistry(heartbeat_timeout_s=15.0, eviction_timeout_s=60.0, clock=clock)
    for i in (1, 2, 3):
        reg.register(
            f"worker-{i}", slots=2, capacity_cpu_units=2048, capacity_memory_mb=4096,
            cores=(2 * (i - 1), 2 * (i - 1) + 1),
        )
    return reg


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #
def test_collector_produces_a_timestamped_sample_per_worker(registry: WorkerRegistry):
    collector = TelemetryCollector(registry, interval_s=0.05)
    samples = collector.collect_once()

    assert {s.worker_id for s in samples} == {"worker-1", "worker-2", "worker-3"}
    for sample in samples:
        assert isinstance(sample, WorkerTelemetry)
        assert sample.timestamp is not None
        assert sample.timestamp.tzinfo is not None, "timestamps must be timezone-aware"
        assert 0.0 <= sample.cpu_percent <= 100.0
        assert 0.0 <= sample.memory_percent <= 100.0
        assert sample.available_memory_mb > 0
        assert sample.sample_duration_ms >= 0.0


def test_samples_reach_the_registry_and_the_history_buffer(registry: WorkerRegistry):
    collector = TelemetryCollector(registry, interval_s=0.05)
    collector.collect_once()
    collector.collect_once()

    state = registry.snapshot().by_id("worker-1")
    assert state.observed is not None
    assert len(registry.telemetry_history("worker-1")) == 2


def test_cpu_is_scoped_to_the_workers_own_cores(registry: WorkerRegistry):
    collector = TelemetryCollector(registry, interval_s=0.05)
    sample = next(s for s in collector.collect_once() if s.worker_id == "worker-2")
    # worker-2 was registered with two cores; per-core readings must match that.
    assert len(sample.cpu_percent_per_core) == 2


def test_process_memory_uses_the_workers_own_children(registry: WorkerRegistry):
    import os

    collector = TelemetryCollector(
        registry, interval_s=0.05, pid_provider=lambda w: (os.getpid(),) if w == "worker-1" else ()
    )
    samples = {s.worker_id: s for s in collector.collect_once()}
    assert samples["worker-1"].process_memory_mb > 0
    assert samples["worker-2"].process_memory_mb == 0


def test_process_memory_tolerates_dead_pids():
    assert process_memory_mb((999_999_999,)) == 0.0


def test_active_jobs_come_from_the_provider(registry: WorkerRegistry):
    collector = TelemetryCollector(
        registry, interval_s=0.05, active_jobs_provider=lambda w: 2 if w == "worker-3" else 0
    )
    samples = {s.worker_id: s for s in collector.collect_once()}
    assert samples["worker-3"].active_jobs == 2
    assert samples["worker-1"].active_jobs == 0


def test_overhead_is_measured_not_assumed(registry: WorkerRegistry):
    collector = TelemetryCollector(registry, interval_s=2.0)
    for _ in range(3):
        collector.collect_once()

    stats = collector.stats.as_dict(2.0)
    assert stats["ticks"] == 3
    assert stats["samples"] == 9
    assert stats["errors"] == 0
    assert stats["mean_tick_ms"] > 0
    # Sampling three workers must not be a material fraction of a 2 s interval.
    assert stats["duty_cycle"] < 0.05


def test_duty_cycle_is_zero_before_any_tick():
    assert CollectorStats().duty_cycle(2.0) == 0.0


def test_collector_thread_starts_and_stops(registry: WorkerRegistry):
    collector = TelemetryCollector(registry, interval_s=0.05)
    collector.start()
    try:
        deadline = time.monotonic() + 3.0
        while collector.stats.ticks < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert collector.running
        assert collector.stats.ticks >= 2
    finally:
        collector.stop()
    assert not collector.running


def test_clear_telemetry_drops_observations(registry: WorkerRegistry):
    TelemetryCollector(registry, interval_s=0.05).collect_once()
    assert registry.snapshot().by_id("worker-1").observed is not None

    registry.clear_telemetry()
    state = registry.snapshot().by_id("worker-1")
    assert state.observed is None
    assert state.telemetry_age_s is None
    assert registry.telemetry_history("worker-1") == []


# --------------------------------------------------------------------------- #
# heartbeats and health
# --------------------------------------------------------------------------- #
def test_stale_worker_becomes_unhealthy_and_is_not_eligible(
    registry: WorkerRegistry, clock: FakeClock
):
    from backend.models.workload_helpers import make_job
    from scheduler.base import infeasibility_reason
    from scheduler.views import job_requirements, static_view

    clock.advance(20.0)  # past heartbeat_timeout_s
    state = registry.snapshot().by_id("worker-1")
    assert state.status is WorkerStatus.UNHEALTHY

    reason = infeasibility_reason(job_requirements(make_job()), static_view(state))
    assert reason == "worker not healthy"


def test_worker_is_evicted_after_the_longer_timeout(registry: WorkerRegistry, clock: FakeClock):
    clock.advance(70.0)
    assert registry.snapshot().by_id("worker-1").status is WorkerStatus.DEAD


def test_heartbeat_restores_health(registry: WorkerRegistry, clock: FakeClock):
    clock.advance(20.0)
    assert registry.snapshot().by_id("worker-1").status is WorkerStatus.UNHEALTHY

    registry.heartbeat("worker-1")
    assert registry.snapshot().by_id("worker-1").status is WorkerStatus.HEALTHY


def test_telemetry_also_refreshes_the_heartbeat(registry: WorkerRegistry, clock: FakeClock):
    clock.advance(20.0)
    registry.ingest_telemetry(WorkerTelemetry(worker_id="worker-2"))
    assert registry.snapshot().by_id("worker-2").status is WorkerStatus.HEALTHY


def test_heartbeats_keep_the_fleet_healthy_without_telemetry(
    registry: WorkerRegistry, clock: FakeClock
):
    """The telemetry-OFF control arm depends on this: with no telemetry at all,
    the fleet must stay schedulable."""
    emitter = HeartbeatEmitter(registry, interval_s=0.05)
    for _ in range(4):
        clock.advance(10.0)
        emitter.beat_once()

    fleet = registry.snapshot()
    assert all(w.status is WorkerStatus.HEALTHY for w in fleet.workers)
    assert all(w.observed is None for w in fleet.workers), "no telemetry was collected"
    assert emitter.beats == 12


def test_heartbeat_emitter_respects_liveness_predicate(
    registry: WorkerRegistry, clock: FakeClock
):
    """A simulated worker failure stops heartbeating without being torn down."""
    emitter = HeartbeatEmitter(registry, interval_s=0.05, is_alive=lambda w: w != "worker-2")
    clock.advance(20.0)
    emitter.beat_once()

    fleet = registry.snapshot()
    assert fleet.by_id("worker-1").status is WorkerStatus.HEALTHY
    assert fleet.by_id("worker-2").status is WorkerStatus.UNHEALTHY


def test_draining_status_is_not_overridden_by_liveness(registry: WorkerRegistry):
    registry.set_status("worker-3", WorkerStatus.DRAINING)
    registry.heartbeat("worker-3")
    assert registry.snapshot().by_id("worker-3").status is WorkerStatus.DRAINING


def test_registry_ignores_telemetry_for_unknown_workers(registry: WorkerRegistry):
    registry.ingest_telemetry(WorkerTelemetry(worker_id="worker-99"))
    assert registry.telemetry_history("worker-99") == []
