"""Data-plane execution.

Workloads run in a process pool, not a thread pool. CPython's GIL would
serialise CPU-bound work across threads, which would make every load-balancing
measurement in this project meaningless: three "workers" sharing one
interpreter cannot contend for CPU the way three EC2 instances do.
"""

from __future__ import annotations

import os
import time
import traceback
from dataclasses import dataclass

from worker.workloads.registry import run_workload


@dataclass(frozen=True)
class ExecutionResult:
    job_id: str
    execution_id: str
    worker_id: str
    ok: bool
    result: dict | None
    error: str | None
    execution_time_ms: float
    cpu_units: int
    memory_mb: int


#: Thread-pool knobs for every BLAS/OpenMP backend numpy might be linked
#: against. Set before the child imports numpy.
_SINGLE_THREAD_ENV = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
)


def force_single_threaded_math() -> None:
    """Restrict BLAS/OpenMP to one thread.

    numpy's matmul is multi-threaded by default, so one ``matrix_multiply`` job
    would spread across every core in its worker's affinity set. That makes a
    "slot" and a job's declared ``cpu_units`` meaningless, and it lets a single
    job saturate a worker the scheduler believes is half idle -- which would
    invalidate every load-balance measurement in the project. One job, one
    core.
    """
    for key in _SINGLE_THREAD_ENV:
        os.environ.setdefault(key, "1")


def pin_to_cores(cores: list[int] | None) -> None:
    """Pool-child initialiser: pin to a disjoint core set per worker.

    Local workers share one machine, so without pinning "worker CPU%" is not
    an independent signal (docs/limitations.md section 3). Best effort -- not
    every platform supports affinity.
    """
    force_single_threaded_math()
    if not cores:
        return
    try:
        os.sched_setaffinity(0, cores)
    except (AttributeError, OSError):
        pass


def execute(job_type: str, payload: dict, scratch: str | None = None) -> dict:
    """Run one workload in a pool child. Returns a result envelope.

    Exceptions are captured rather than raised so a workload failure is data,
    not a pool-level error that could poison the executor.
    """
    started = time.perf_counter()
    try:
        result = run_workload(job_type, payload, scratch=scratch)
        return {
            "ok": True,
            "result": result,
            "error": None,
            "duration_ms": (time.perf_counter() - started) * 1000.0,
            "pid": os.getpid(),
        }
    except Exception as exc:  # noqa: BLE001 -- the boundary is intentional
        return {
            "ok": False,
            "result": None,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(limit=5),
            "duration_ms": (time.perf_counter() - started) * 1000.0,
            "pid": os.getpid(),
        }
