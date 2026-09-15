from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import pytest

from backend.config import NimbusConfig, set_config
from backend.models.enums import WorkerStatus
from backend.models.telemetry import ObservedWindow, WorkerTelemetry
from backend.models.worker import (
    DeclaredCapacity,
    FleetSnapshot,
    ReservedResources,
    WorkerState,
)
from backend.services.engine import NimbusEngine


@pytest.fixture
def config(tmp_path: Path) -> NimbusConfig:
    cfg = NimbusConfig(
        backend="local",
        strategy="fifo",
        worker_count=3,
        worker_slots=2,
        scheduler_tick_s=0.01,
        visibility_timeout_s=30.0,
        max_attempts=3,
        artifact_dir=str(tmp_path / "artifacts"),
        telemetry_enabled=True,
        telemetry_interval_s=0.25,
        heartbeat_interval_s=1.0,
        seed=1337,
    )
    set_config(cfg)
    return cfg


# --------------------------------------------------------------------------- #
# fleet fixtures -- shared by every placement test
# --------------------------------------------------------------------------- #
def worker_state(
    worker_id: str,
    *,
    slots: int = 2,
    cpu_capacity: int = 2048,
    memory_capacity: int = 4096,
    cpu_reserved: int = 0,
    memory_reserved: int = 0,
    active_jobs: int = 0,
    status: WorkerStatus = WorkerStatus.HEALTHY,
    observed: WorkerTelemetry | None = None,
    heartbeat_age_s: float = 0.0,
) -> WorkerState:
    return WorkerState(
        worker_id=worker_id,
        status=status,
        declared=DeclaredCapacity(
            cpu_units=cpu_capacity, memory_mb=memory_capacity, slots=slots
        ),
        reserved=ReservedResources(
            cpu_units=cpu_reserved, memory_mb=memory_reserved, active_jobs=active_jobs
        ),
        observed=observed,
        heartbeat_age_s=heartbeat_age_s,
        telemetry_age_s=None if observed is None else 0.0,
    )


def telemetry(worker_id: str, *, cpu: float = 0.0, memory: float = 0.0, **kwargs) -> WorkerTelemetry:
    return WorkerTelemetry(
        worker_id=worker_id, cpu_percent=cpu, memory_percent=memory, **kwargs
    )


def observed_worker(
    worker_id: str,
    *,
    cpu: float = 0.0,
    memory: float = 0.0,
    process_memory_mb: float = 0.0,
    trend: float = 0.0,
    samples: int = 3,
    age_s: float | None = 0.5,
    **static_kwargs,
) -> WorkerState:
    """A worker with fresh, self-consistent OBSERVED state.

    ``age_s=None`` models telemetry that was never collected.
    """
    if age_s is None:
        return worker_state(worker_id, **static_kwargs)
    return worker_state(
        worker_id,
        observed=WorkerTelemetry(
            worker_id=worker_id,
            cpu_percent=cpu,
            memory_percent=memory,
            process_memory_mb=process_memory_mb,
            available_memory_mb=4000.0,
        ),
        **static_kwargs,
    ).model_copy(
        update={
            "observed_window": ObservedWindow(
                cpu_percent_mean=cpu,
                cpu_percent_latest=cpu,
                cpu_trend_pct_per_s=trend,
                samples=samples,
            ),
            "telemetry_age_s": age_s,
        }
    )


def make_fleet(*workers: WorkerState) -> FleetSnapshot:
    return FleetSnapshot(workers=tuple(workers))


@pytest.fixture
def engine(config: NimbusConfig):
    eng = NimbusEngine(config)
    eng.start()
    try:
        yield eng
    finally:
        eng.stop(wait=False)
        shutil.rmtree(".nimbus/scratch", ignore_errors=True)


#: Small, fast parameters for every registered job type.
SMALL_PAYLOADS = {
    "matrix_multiply": {"size": 120},
    "prime_count": {"limit": 50_000},
    "hash_chain": {"rounds": 20_000},
    "sort_large": {"elements": 200_000},
    "array_transform": {"mb": 8},
    "compress_file": {"mb": 2},
    "file_scan": {"mb": 2},
    "json_transform": {"records": 500},
    "metadata_extract": {"count": 30},
}
