"""Worker state, split into the three categories the experiment depends on.

    DECLARED  DeclaredCapacity   -- configured, static
    RESERVED  ReservedResources  -- Nimbus's own bookkeeping
    OBSERVED  WorkerTelemetry    -- psutil, may be absent

``WorkerState`` is the full record. Placement policies never receive it; they
receive a projection that exposes only the categories they are allowed to see
(scheduler/views.py).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from backend.models.enums import WorkerStatus
from backend.models.job import utcnow
from backend.models.telemetry import ObservedWindow, WorkerTelemetry


class DeclaredCapacity(BaseModel):
    """What the operator says the worker has. Never inferred from telemetry."""

    model_config = {"frozen": True}

    cpu_units: int = 2048  # ECS-style units; 1 vCPU = 1024
    memory_mb: int = 4096
    slots: int = 2


class ReservedResources(BaseModel):
    """What Nimbus has already committed to the worker."""

    model_config = {"frozen": True}

    cpu_units: int = 0
    memory_mb: int = 0
    active_jobs: int = 0


class WorkerState(BaseModel):
    """Complete control-plane view of one worker."""

    model_config = {"frozen": True}

    worker_id: str
    status: WorkerStatus = WorkerStatus.HEALTHY
    declared: DeclaredCapacity = Field(default_factory=DeclaredCapacity)
    reserved: ReservedResources = Field(default_factory=ReservedResources)
    observed: WorkerTelemetry | None = None
    #: Smoothed summary over the last N samples. OBSERVED, like ``observed``.
    observed_window: ObservedWindow | None = None
    heartbeat_age_s: float = 0.0
    telemetry_age_s: float | None = None

    @property
    def healthy(self) -> bool:
        return self.status is WorkerStatus.HEALTHY

    @property
    def free_slots(self) -> int:
        return max(0, self.declared.slots - self.reserved.active_jobs)

    @property
    def available_cpu_units(self) -> int:
        return self.declared.cpu_units - self.reserved.cpu_units

    @property
    def available_memory_mb(self) -> int:
        return self.declared.memory_mb - self.reserved.memory_mb


class FleetSnapshot(BaseModel):
    """An immutable point-in-time view of the fleet.

    One snapshot backs one placement decision, which is what makes placement a
    pure function and decision traces replayable.
    """

    model_config = {"frozen": True}

    taken_at: datetime = Field(default_factory=utcnow)
    workers: tuple[WorkerState, ...] = ()

    def by_id(self, worker_id: str) -> WorkerState | None:
        return next((w for w in self.workers if w.worker_id == worker_id), None)

    def healthy(self) -> tuple[WorkerState, ...]:
        return tuple(w for w in self.workers if w.healthy)
