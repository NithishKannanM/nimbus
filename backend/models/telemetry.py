"""Observed worker telemetry.

This module holds the OBSERVED half of a worker's state. It is deliberately a
separate type from declared capacity and Nimbus reservations, because the whole
Phase 2 / Phase 3 experiment turns on keeping those three categories apart:

    DECLARED  what the operator says a worker has        (static, configured)
    RESERVED  what Nimbus has already committed to it    (static, bookkeeping)
    OBSERVED  what the host is actually doing            (telemetry, psutil)

Static Resource-Aware placement may read DECLARED and RESERVED. It must not be
able to read OBSERVED -- see scheduler/views.py, where that is enforced by
construction rather than by convention.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.models.job import utcnow


class WorkerTelemetry(BaseModel):
    """One psutil sample for one worker.

    Fields are grouped by *scope*, which matters because a local worker is a
    pinned process pool on a shared machine rather than its own EC2 instance:

    worker-scoped
        Derived from this worker's CPU affinity set and its own pool children.
        These are the fields that discriminate between local workers, and they
        are the local analogue of a per-instance metric.

    host-scoped
        Read from the whole machine, so on a local fleet every worker reports
        the same value. On EC2 (Phase 5) each worker owns its host and these
        become per-worker signals. Recorded now, with the limitation documented
        in docs/limitations.md, rather than faked.
    """

    model_config = {"frozen": True}

    worker_id: str
    timestamp: datetime = Field(default_factory=utcnow)

    # -- worker-scoped ---------------------------------------------------
    cpu_percent: float = 0.0
    """Mean utilisation across the cores this worker is pinned to."""

    cpu_percent_per_core: tuple[float, ...] = ()
    process_memory_mb: float = 0.0
    """Resident set size of the worker's pool children."""

    active_jobs: int = 0

    # -- host-scoped -----------------------------------------------------
    memory_percent: float = 0.0
    available_memory_mb: float = 0.0
    load_average_1m: float = 0.0

    # -- meta ------------------------------------------------------------
    sample_duration_ms: float = 0.0
    """Cost of taking this sample. Telemetry overhead has to be measured, not
    assumed (docs/experiments.md)."""

    def summary(self) -> str:
        return (
            f"cpu={self.cpu_percent:5.1f}% mem={self.memory_percent:5.1f}% "
            f"avail={self.available_memory_mb:7.0f}MB load={self.load_average_1m:4.2f}"
        )


class ObservedWindow(BaseModel):
    """Derived summary of the last N telemetry samples for one worker.

    Still OBSERVED state -- static placement cannot see this any more than it
    can see a raw sample. It exists because a single instantaneous reading is
    noisy, and adaptive-v1 pre-registered a fixed 3-sample smoothing window
    (docs/adaptive-v1-preregistration.md section 2.3).

    Computed by the registry, which owns the history buffer, so that placement
    policies stay pure functions of the snapshot they are handed.
    """

    model_config = {"frozen": True}

    cpu_percent_mean: float = 0.0
    """Arithmetic mean over the window -- the value adaptive-v1 scores on."""

    cpu_percent_latest: float = 0.0
    cpu_trend_pct_per_s: float = 0.0
    """Least-squares slope of CPU% against time. Positive = rising = worse."""

    samples: int = 0
    span_s: float = 0.0
