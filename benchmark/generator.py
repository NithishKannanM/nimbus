"""Deterministic job-sequence generation.

``generate(profile, job_count, seed)`` is a pure function: the same triple
always produces a byte-identical list, which is what lets two strategies run on
literally the same workload rather than on two samples from the same
distribution. Asserted by tests/unit/test_generator.py.
"""

from __future__ import annotations

import random
from dataclasses import asdict, dataclass
from typing import Any

from backend.models.enums import WorkloadClass
from benchmark.profiles import Profile, get_profile
from worker.workloads.registry import REGISTRY

#: Intensity ranges per job type. Chosen so a job takes roughly 20 ms to 2 s on
#: one core, given that workers pin BLAS to a single thread.
PARAM_RANGES: dict[str, dict[str, tuple[int, int]]] = {
    "matrix_multiply": {"size": (300, 1400)},
    "prime_count": {"limit": (100_000, 5_000_000)},
    "hash_chain": {"rounds": (100_000, 2_000_000)},
    "sort_large": {"elements": (1_000_000, 20_000_000)},
    "array_transform": {"mb": (64, 384)},
    "compress_file": {"mb": (8, 96)},
    "file_scan": {"mb": (16, 256)},
    "json_transform": {"records": (100, 5_000)},
    "metadata_extract": {"count": (10, 200)},
}

#: Priorities, weighted towards Normal.
PRIORITY_WEIGHTS = ((1, 0.10), (2, 0.20), (3, 0.50), (4, 0.20))

#: Pareto shape. Lower alpha => heavier tail.
PARETO_ALPHA = 1.5


@dataclass(frozen=True)
class PlannedJob:
    index: int
    job_type: str
    workload_class: WorkloadClass
    payload: dict[str, Any]
    priority: int
    arrival_offset_s: float

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["workload_class"] = self.workload_class.value
        return data


def _types_by_class() -> dict[WorkloadClass, list[str]]:
    grouped: dict[WorkloadClass, list[str]] = {c: [] for c in WorkloadClass}
    for spec in REGISTRY.values():
        grouped[spec.workload_class].append(spec.job_type)
    return {k: sorted(v) for k, v in grouped.items()}


def _weighted_choice(rng: random.Random, weights: dict[Any, float]) -> Any:
    roll = rng.random()
    cumulative = 0.0
    for key, weight in weights.items():
        cumulative += weight
        if roll < cumulative:
            return key
    return next(reversed(weights))


def _sample_fraction(rng: random.Random, distribution: str) -> float:
    """A value in [0, 1] locating a job within its parameter range."""
    if distribution == "uniform":
        return rng.random()
    # Pareto, rescaled into [0, 1]: most draws sit low, a few reach the top.
    raw = (1.0 - rng.random()) ** (-1.0 / PARETO_ALPHA) - 1.0
    return min(1.0, raw / 10.0)


def _arrival_offsets(rng: random.Random, profile: Profile, count: int) -> list[float]:
    if profile.arrival == "burst" and profile.burst is not None:
        burst = profile.burst
        offsets: list[float] = []
        clock = 0.0
        steady = max(0, count - burst.burst_jobs)
        for i in range(steady):
            clock += rng.expovariate(burst.quiet_rate_per_s)
            if clock > burst.burst_at_s and len(offsets) < steady:
                clock = max(clock, burst.burst_at_s + 0.001)
            offsets.append(clock)
        # The burst arrives together, at burst_at_s.
        offsets.extend([burst.burst_at_s] * (count - steady))
        return sorted(offsets)

    offsets = []
    clock = 0.0
    for _ in range(count):
        clock += rng.expovariate(profile.rate_per_s)
        offsets.append(clock)
    return offsets


def generate(
    profile: Profile | str, job_count: int, seed: int, *, size_scale: float = 1.0
) -> list[PlannedJob]:
    """Build a reproducible job sequence.

    ``size_scale`` shrinks every intensity parameter proportionally so a demo
    can run in seconds without changing the *shape* of the distribution. It is
    recorded in the manifest, because a run at 0.2 scale is not comparable with
    a run at 1.0.
    """
    if isinstance(profile, str):
        profile = get_profile(profile)

    rng = random.Random(seed)
    grouped = _types_by_class()
    mix = profile.normalised_mix()
    priorities = dict(PRIORITY_WEIGHTS)

    offsets = _arrival_offsets(random.Random(seed ^ 0x5EED), profile, job_count)

    planned: list[PlannedJob] = []
    for index in range(job_count):
        workload_class = _weighted_choice(rng, mix)
        job_type = grouped[workload_class][rng.randrange(len(grouped[workload_class]))]

        payload: dict[str, Any] = {}
        for name, (low, high) in PARAM_RANGES[job_type].items():
            fraction = _sample_fraction(rng, profile.size_distribution)
            value = low + fraction * (high - low)
            payload[name] = max(low, int(value * size_scale))

        planned.append(
            PlannedJob(
                index=index,
                job_type=job_type,
                workload_class=workload_class,
                payload=payload,
                priority=_weighted_choice(rng, priorities),
                arrival_offset_s=round(offsets[index], 6),
            )
        )
    return planned


def class_histogram(jobs: list[PlannedJob]) -> dict[str, int]:
    counts: dict[str, int] = {c.value: 0 for c in WorkloadClass}
    for job in jobs:
        counts[job.workload_class.value] += 1
    return counts
