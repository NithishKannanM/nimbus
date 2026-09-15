"""The scheduling decision trace -- Phase 2's headline deliverable.

A trace must let someone reconstruct a placement long after the fact: what the
job needed, what every worker declared, what Nimbus had reserved, what the host
was *actually* doing, which workers were eligible, what they scored, and why one
was chosen.

The trace records OBSERVED telemetry for every worker. The static placement
policy never saw it: the trace is assembled by ``SchedulingStrategy.place``
*after* the policy has returned, from the full fleet snapshot, while the policy
only ever held a ``StaticWorkerView``. Recording and consuming are different
things, and here they are different code paths.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from backend.models.enums import WorkloadClass
from backend.models.job import utcnow
from backend.models.telemetry import ObservedWindow, WorkerTelemetry


class JobRequirementsRecord(BaseModel):
    """What the placement policy was told about the job."""

    model_config = {"frozen": True}

    job_type: str
    workload_class: WorkloadClass
    priority: int
    cpu_units: int
    memory_mb: int
    attempts: int


class CandidateRecord(BaseModel):
    """One worker's full state at decision time, plus how it scored.

    DECLARED / RESERVED were available to the policy.
    OBSERVED was not -- it is recorded here for later analysis only.
    """

    eligible: bool
    ineligible_reason: str | None = None
    #: Liveness at decision time. Recorded so a trace is self-describing and a
    #: decision can be replayed exactly (scheduler/replay.py).
    healthy: bool = True

    # DECLARED -- static, configured
    declared_cpu_units: int
    declared_memory_mb: int
    declared_slots: int

    # RESERVED -- Nimbus bookkeeping
    reserved_cpu_units: int
    reserved_memory_mb: int
    active_jobs: int

    # OBSERVED -- recorded for every policy; consumed only by adaptive-v1
    observed: WorkerTelemetry | None = None
    #: Smoothed summary over the pre-registered window. This, not ``observed``,
    #: is what adaptive-v1 actually scores on, so replay needs it.
    observed_window: ObservedWindow | None = None
    observed_age_s: float | None = None
    #: Telemetry quality as judged at decision time. Recorded because a stale
    #: reading and a fresh one produce different scores from the same numbers.
    telemetry_fresh: bool = False

    # scoring (eligible candidates only)
    score: float | None = None
    terms: dict[str, float] = Field(default_factory=dict)


class SchedulingDecision(BaseModel):
    decision_id: str = Field(default_factory=lambda: f"dec-{uuid.uuid4().hex[:12]}")
    job_id: str
    attempt: int

    #: Composite strategy name, e.g. "static_resource_aware".
    strategy: str
    #: Placement algorithm identity, e.g. "adaptive-v1". Never overwritten:
    #: a changed formula is a new version, so archived comparisons stay valid.
    algorithm_version: str = "unversioned"
    #: The two halves, recorded separately so an analysis can group by either.
    ordering_policy: str
    placement_policy: str

    workload_class: WorkloadClass
    priority: int
    effective_priority: int
    job: JobRequirementsRecord | None = None

    decided_at: datetime = Field(default_factory=utcnow)
    decision_latency_us: float = 0.0
    fleet_snapshot_at: datetime | None = None

    #: Whether telemetry collection was running. Placement must be identical
    #: either way for static policies; this field is what proves it.
    telemetry_enabled: bool = False

    weights: dict[str, float] | None = None
    candidates: dict[str, CandidateRecord] = Field(default_factory=dict)
    excluded: dict[str, str] = Field(default_factory=dict)

    selected_worker: str | None = None
    runner_up: str | None = None
    margin: float | None = None
    reason: str = ""

    # -- convenience for analysis and tests ------------------------------
    @property
    def eligible_workers(self) -> list[str]:
        return sorted(w for w, c in self.candidates.items() if c.eligible)

    def observed_cpu(self, worker_id: str) -> float | None:
        candidate = self.candidates.get(worker_id)
        if candidate is None or candidate.observed is None:
            return None
        return candidate.observed.cpu_percent

    def component(self, worker_id: str, name: str) -> float | None:
        candidate = self.candidates.get(worker_id)
        return None if candidate is None else candidate.terms.get(name)
