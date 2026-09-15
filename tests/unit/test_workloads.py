"""Workloads must be deterministic (benchmarks depend on it) and their payload
bounds must actually bind (a payload is untrusted input that decides how much
memory a worker allocates)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from backend.models.enums import WorkloadClass
from tests.conftest import SMALL_PAYLOADS
from worker.workloads.registry import (
    JOB_TYPES,
    UnknownJobType,
    classify,
    requirements,
    run_workload,
    validate,
)


def test_every_registered_type_has_a_test_payload():
    assert set(SMALL_PAYLOADS) == set(JOB_TYPES)


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_workload_runs_and_returns_a_checksum(job_type: str):
    result = run_workload(job_type, SMALL_PAYLOADS[job_type])
    assert "checksum" in result


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_workload_is_deterministic(job_type: str):
    payload = SMALL_PAYLOADS[job_type]
    assert run_workload(job_type, payload) == run_workload(job_type, payload)


@pytest.mark.parametrize("job_type", JOB_TYPES)
def test_defaults_are_valid(job_type: str):
    _spec, parsed = validate(job_type, {})
    assert parsed.model_dump()


def test_all_four_workload_classes_are_represented():
    assert {classify(t) for t in JOB_TYPES} == set(WorkloadClass)


def test_unknown_job_type_is_rejected():
    with pytest.raises(UnknownJobType):
        run_workload("rm_minus_rf", {})


def test_payload_bounds_are_enforced():
    with pytest.raises(ValidationError):
        validate("sort_large", {"elements": 10**12})
    with pytest.raises(ValidationError):
        validate("matrix_multiply", {"size": 0})
    with pytest.raises(ValidationError):
        validate("compress_file", {"mb": 4, "level": 42})


def test_unknown_payload_fields_are_rejected():
    with pytest.raises(ValidationError):
        validate("prime_count", {"limit": 5000, "command": "whoami"})


def test_declared_memory_scales_with_payload():
    """Declared requirements are what Scheduler C filters on, so they have to
    track the payload rather than being a constant."""
    sizes = [2_000_000, 8_000_000, 32_000_000]
    declared = [requirements("sort_large", {"elements": n})[1] for n in sizes]
    assert declared == sorted(declared)
    assert declared[-1] > declared[0] * 4


def test_prime_count_is_actually_correct():
    assert run_workload("prime_count", {"limit": 1_000})["primes"] == 168
    assert run_workload("prime_count", {"limit": 10_000})["primes"] == 1229
    assert run_workload("prime_count", {"limit": 10_000})["largest_prime"] == 9973


def test_compression_actually_compresses_but_not_trivially():
    ratio = run_workload("compress_file", {"mb": 2})["ratio"]
    assert 0.3 < ratio < 0.95, "blob should be realistically compressible"
