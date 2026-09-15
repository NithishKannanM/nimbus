"""Deterministic workload profiles.

Each profile fixes a class mix, an arrival process, a size distribution and any
*external* host pressure. A profile plus a seed plus a job count fully
determines the job sequence, so two strategies can be compared on a literally
identical workload (docs/experiments.md section 2.1).

Phase 2 uses these to establish baseline behaviour. Notably, Profile A is
expected to show static placement doing well: on a homogeneous fleet under
saturation there is little for any scheduler to exploit. That is a result, not
a failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.models.enums import WorkloadClass

ArrivalProcess = Literal["poisson", "burst"]
SizeDistribution = Literal["uniform", "pareto"]
PressureKind = Literal["cpu", "memory"]


@dataclass(frozen=True)
class PressureSpec:
    """External host pressure, applied outside Nimbus.

    Critically *not* a Nimbus job: if Nimbus generated the load it would appear
    as a reservation and the static scheduler would already know about it. The
    experiment is specifically about what happens when real host state diverges
    from Nimbus's declared/reserved model (docs/experiments.md section 4.1).
    """

    kind: PressureKind
    target_worker: str
    #: CPU: worker load as a fraction of its cores. Memory: MB to hold resident.
    intensity: float
    start_offset_s: float = 0.0
    duration_s: float | None = None


@dataclass(frozen=True)
class BurstSpec:
    quiet_rate_per_s: float
    burst_jobs: int
    burst_at_s: float


@dataclass(frozen=True)
class Profile:
    name: str
    description: str
    mix: dict[WorkloadClass, float]
    arrival: ArrivalProcess = "poisson"
    rate_per_s: float = 6.0
    size_distribution: SizeDistribution = "uniform"
    burst: BurstSpec | None = None
    pressure: tuple[PressureSpec, ...] = field(default_factory=tuple)

    def normalised_mix(self) -> dict[WorkloadClass, float]:
        total = sum(self.mix.values())
        return {k: v / total for k, v in self.mix.items()}


BALANCED = {
    WorkloadClass.CPU_BOUND: 0.25,
    WorkloadClass.MEMORY_BOUND: 0.25,
    WorkloadClass.IO_BOUND: 0.25,
    WorkloadClass.LIGHTWEIGHT: 0.25,
}
CPU_HEAVY = {
    WorkloadClass.CPU_BOUND: 0.70,
    WorkloadClass.MEMORY_BOUND: 0.10,
    WorkloadClass.IO_BOUND: 0.10,
    WorkloadClass.LIGHTWEIGHT: 0.10,
}
MEMORY_HEAVY = {
    WorkloadClass.CPU_BOUND: 0.10,
    WorkloadClass.MEMORY_BOUND: 0.70,
    WorkloadClass.IO_BOUND: 0.10,
    WorkloadClass.LIGHTWEIGHT: 0.10,
}
LONG_TAIL_MIX = {
    WorkloadClass.CPU_BOUND: 0.40,
    WorkloadClass.MEMORY_BOUND: 0.20,
    WorkloadClass.IO_BOUND: 0.30,
    WorkloadClass.LIGHTWEIGHT: 0.10,
}


PROFILES: dict[str, Profile] = {
    p.name: p
    for p in (
        Profile(
            name="homogeneous_saturated",
            description=(
                "Profile A. Identical workers, no external pressure, steady arrivals, "
                "mixed classes. Static placement is expected to do well here; that is "
                "the control, not a failure."
            ),
            mix=BALANCED,
            rate_per_s=8.0,
        ),
        Profile(
            name="cpu_pressure",
            description=(
                "Profile B. External CPU load on worker-1, invisible to Nimbus's "
                "declared/reserved model. Static placement must NOT react; telemetry "
                "must record it. This is the profile Phase 3 exists to exploit."
            ),
            mix=CPU_HEAVY,
            rate_per_s=6.0,
            pressure=(PressureSpec(kind="cpu", target_worker="worker-1", intensity=1.0),),
        ),
        Profile(
            name="memory_pressure",
            description=(
                "Profile C. External memory consumption on the host. Static placement "
                "remains unaware; telemetry observes the pressure."
            ),
            mix=MEMORY_HEAVY,
            rate_per_s=4.0,
            pressure=(
                PressureSpec(kind="memory", target_worker="worker-1", intensity=1024.0),
            ),
        ),
        Profile(
            name="heavy_tail",
            description=(
                "Profile D. Pareto-distributed job sizes: many short, some medium, a "
                "few very long. Stresses head-of-line blocking."
            ),
            mix=LONG_TAIL_MIX,
            rate_per_s=6.0,
            size_distribution="pareto",
        ),
        Profile(
            name="burst",
            description=(
                "Profile E. Low steady traffic, a sudden burst, then a return to "
                "normal. Measures queue behaviour under a step change in load."
            ),
            mix=BALANCED,
            arrival="burst",
            rate_per_s=2.0,
            burst=BurstSpec(quiet_rate_per_s=2.0, burst_jobs=30, burst_at_s=4.0),
        ),
    )
}

PROFILE_NAMES = tuple(PROFILES)


def get_profile(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"unknown profile {name!r}; available: {', '.join(PROFILE_NAMES)}"
        ) from None
