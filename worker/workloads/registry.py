"""The closed workload registry.

``job_type`` is an enum-like key into this table. A payload can choose
parameters; it can never choose code. Adding a workload means adding an entry
here, which is the only extension point.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from backend.models.enums import WorkloadClass
from worker.workloads import cpu, io, light, memory, params


@dataclass(frozen=True)
class WorkloadSpec:
    job_type: str
    workload_class: WorkloadClass
    params_model: type
    fn: Callable[..., dict]
    #: (validated params) -> declared (cpu_units, memory_mb). These are the
    #: ECS-style declared requirements the resource-aware scheduler filters on.
    requirements: Callable[[Any], tuple[int, int]]
    needs_scratch: bool = False


def _fixed(cpu_units: int, memory_mb: int) -> Callable[[Any], tuple[int, int]]:
    return lambda _p: (cpu_units, memory_mb)


REGISTRY: dict[str, WorkloadSpec] = {
    spec.job_type: spec
    for spec in (
        WorkloadSpec(
            "matrix_multiply", WorkloadClass.CPU_BOUND, params.MatrixMultiplyParams,
            cpu.matrix_multiply,
            # three size x size float64 matrices, plus slack
            lambda p: (1024, max(128, int(3 * p.size**2 * 8 / 1e6 * 1.3))),
        ),
        WorkloadSpec(
            "prime_count", WorkloadClass.CPU_BOUND, params.PrimeCountParams,
            cpu.prime_count,
            lambda p: (1024, max(64, int(p.limit / 1e6 * 1.2) + 32)),
        ),
        WorkloadSpec(
            "hash_chain", WorkloadClass.CPU_BOUND, params.HashChainParams,
            cpu.hash_chain, _fixed(1024, 64),
        ),
        WorkloadSpec(
            "sort_large", WorkloadClass.MEMORY_BOUND, params.SortLargeParams,
            memory.sort_large,
            lambda p: (512, max(64, int(p.elements * 8 / 1e6 * 1.6))),
        ),
        WorkloadSpec(
            "array_transform", WorkloadClass.MEMORY_BOUND, params.ArrayTransformParams,
            memory.array_transform,
            lambda p: (512, max(128, int(p.mb * 2.5))),
        ),
        WorkloadSpec(
            "compress_file", WorkloadClass.IO_BOUND, params.CompressFileParams,
            io.compress_file, lambda p: (384, 128), needs_scratch=True,
        ),
        WorkloadSpec(
            "file_scan", WorkloadClass.IO_BOUND, params.FileScanParams,
            io.file_scan, lambda p: (256, 128), needs_scratch=True,
        ),
        WorkloadSpec(
            "json_transform", WorkloadClass.LIGHTWEIGHT, params.JsonTransformParams,
            light.json_transform,
            lambda p: (256, max(64, int(p.records / 1000) * 16 + 64)),
        ),
        WorkloadSpec(
            "metadata_extract", WorkloadClass.LIGHTWEIGHT, params.MetadataExtractParams,
            light.metadata_extract, _fixed(128, 64), needs_scratch=True,
        ),
    )
}

JOB_TYPES = tuple(REGISTRY)


class UnknownJobType(KeyError):
    pass


def get_spec(job_type: str) -> WorkloadSpec:
    try:
        return REGISTRY[job_type]
    except KeyError:
        raise UnknownJobType(
            f"unknown job_type {job_type!r}; known types: {', '.join(JOB_TYPES)}"
        ) from None


def validate(job_type: str, payload: dict[str, Any]):
    """Validate a payload and return (spec, validated params)."""
    spec = get_spec(job_type)
    return spec, spec.params_model(**payload)


def classify(job_type: str) -> WorkloadClass:
    return get_spec(job_type).workload_class


def requirements(job_type: str, payload: dict[str, Any]) -> tuple[int, int]:
    spec, parsed = validate(job_type, payload)
    return spec.requirements(parsed)


def run_workload(job_type: str, payload: dict[str, Any], scratch: str | None = None) -> dict:
    """Execute a workload. Top-level and picklable: this is the function the
    worker's process pool actually calls."""
    spec, parsed = validate(job_type, payload)
    kwargs = parsed.model_dump()
    if spec.needs_scratch:
        kwargs["scratch"] = scratch
    return spec.fn(**kwargs)
