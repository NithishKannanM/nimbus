"""Workload generation must be reproducible: two strategies have to run on a
literally identical job sequence, not two samples from the same distribution."""

from __future__ import annotations

import pytest

from backend.models.enums import WorkloadClass
from benchmark.generator import PARAM_RANGES, class_histogram, generate
from benchmark.profiles import PROFILE_NAMES, get_profile
from worker.workloads.registry import JOB_TYPES, validate


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_generation_is_deterministic(profile: str):
    first = [j.as_dict() for j in generate(profile, 50, seed=1337)]
    second = [j.as_dict() for j in generate(profile, 50, seed=1337)]
    assert first == second


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_different_seeds_give_different_sequences(profile: str):
    a = [j.as_dict() for j in generate(profile, 50, seed=1)]
    b = [j.as_dict() for j in generate(profile, 50, seed=2)]
    assert a != b


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_every_generated_payload_validates(profile: str):
    for job in generate(profile, 40, seed=7):
        spec, parsed = validate(job.job_type, job.payload)
        assert spec.workload_class is job.workload_class
        assert parsed.model_dump()


@pytest.mark.parametrize("profile", PROFILE_NAMES)
def test_arrivals_are_monotonic_and_non_negative(profile: str):
    offsets = [j.arrival_offset_s for j in generate(profile, 40, seed=11)]
    assert offsets == sorted(offsets)
    assert offsets[0] >= 0


def test_size_scale_shrinks_without_changing_shape():
    full = generate("homogeneous_saturated", 40, seed=3)
    scaled = generate("homogeneous_saturated", 40, seed=3, size_scale=0.25)
    assert [j.job_type for j in full] == [j.job_type for j in scaled]
    assert [j.priority for j in full] == [j.priority for j in scaled]
    # Same jobs, smaller parameters (bounded below by each range's minimum).
    assert any(
        s.payload[k] < full[i].payload[k]
        for i, s in enumerate(scaled)
        for k in s.payload
    )


def test_payloads_respect_declared_parameter_ranges():
    for job in generate("heavy_tail", 60, seed=5):
        for name, value in job.payload.items():
            low, high = PARAM_RANGES[job.job_type][name]
            assert low <= value <= high, f"{job.job_type}.{name}={value} outside [{low},{high}]"


def test_every_job_type_has_a_declared_range():
    assert set(PARAM_RANGES) == set(JOB_TYPES)


def test_cpu_heavy_profile_is_actually_cpu_heavy():
    histogram = class_histogram(generate("cpu_pressure", 300, seed=13))
    fraction = histogram[WorkloadClass.CPU_BOUND.value] / 300
    assert 0.60 < fraction < 0.80, f"expected ~70% CPU_BOUND, got {fraction:.2%}"


def test_burst_profile_delivers_a_simultaneous_burst():
    profile = get_profile("burst")
    jobs = generate(profile, 60, seed=17)
    at_burst = [j for j in jobs if j.arrival_offset_s == profile.burst.burst_at_s]
    assert len(at_burst) == profile.burst.burst_jobs


def test_pressure_profiles_declare_external_pressure():
    assert get_profile("cpu_pressure").pressure[0].kind == "cpu"
    assert get_profile("memory_pressure").pressure[0].kind == "memory"
    assert get_profile("homogeneous_saturated").pressure == ()


def test_unknown_profile_is_rejected():
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("adaptive_wins")
