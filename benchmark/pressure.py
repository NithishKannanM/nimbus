"""External host pressure generators.

The whole point of Profiles B and C is to make the *host's real state* diverge
from Nimbus's declared/reserved model. That only works if the interference is
genuinely external: if Nimbus produced the load as a job, it would appear as a
reservation and the static scheduler would already know about it, and the
experiment would be measuring nothing.

So pressure is applied by plain OS subprocesses that never import Nimbus, hold
no lease, and consume no slot. From Nimbus's point of view the worker still has
its full declared capacity free. Only telemetry can see the difference -- which
is precisely the hypothesis Phase 3 will test.

Backends
--------
``builtin`` (default)
    ``sys.executable -c '<busy loop>'``, pinned with ``os.sched_setaffinity`` to
    the target worker's cores. No external dependency, so an experiment
    reproduces on any machine with Python.

``stress-ng``
    Used when ``NIMBUS_PRESSURE_BACKEND=stress-ng`` and the binary is present.
    More precise load shaping, but its availability and version vary by host,
    so it is not the default.

Pinning to the *target worker's core set* is essential: on a 20-core machine an
unpinned stressor would land anywhere and perturb every worker equally, which
would produce a uniform shift rather than the asymmetry the experiment needs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

# Busy loop. Deliberately arithmetic-only so it competes for CPU rather than
# memory bandwidth or the allocator.
_CPU_CODE = """
import os, sys, time
cores = [int(c) for c in sys.argv[1].split(',') if c != '']
deadline = float(sys.argv[2])
try:
    os.sched_setaffinity(0, cores)
except (AttributeError, OSError):
    pass
x = 0.0
while deadline <= 0 or time.time() < deadline:
    for _ in range(200000):
        x = x * 1.0000001 + 1.0
"""

# Allocates and repeatedly touches a block so the pages stay resident and the
# host's available-memory figure genuinely drops.
_MEM_CODE = """
import os, sys, time
megabytes = int(sys.argv[1])
deadline = float(sys.argv[2])
block = bytearray(megabytes * 1024 * 1024)
step = 4096
while deadline <= 0 or time.time() < deadline:
    for i in range(0, len(block), step * 64):
        block[i] = (block[i] + 1) % 251
    time.sleep(0.05)
"""


def backend_name() -> str:
    requested = os.environ.get("NIMBUS_PRESSURE_BACKEND", "builtin").strip()
    if requested == "stress-ng" and shutil.which("stress-ng"):
        return "stress-ng"
    return "builtin"


@dataclass
class ExternalPressure:
    """A running external stressor. Always stop it: these are real processes."""

    kind: str
    target_worker: str
    cores: tuple[int, ...]
    intensity: float
    backend: str
    processes: list[subprocess.Popen] = field(default_factory=list)
    started_at: float = field(default_factory=time.monotonic)

    def stop(self, timeout: float = 3.0) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + timeout
        for process in self.processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)
        self.processes.clear()

    @property
    def alive(self) -> int:
        return sum(1 for p in self.processes if p.poll() is None)

    def describe(self) -> str:
        return (
            f"{self.kind} pressure on {self.target_worker} "
            f"(cores {list(self.cores)}, intensity {self.intensity}, "
            f"backend {self.backend}, {len(self.processes)} process(es))"
        )


def _spawn(args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )


def start_cpu_pressure(
    target_worker: str, cores: tuple[int, ...], *, intensity: float = 1.0,
    duration_s: float | None = None,
) -> ExternalPressure:
    """Load ``intensity`` (0..1) of the target worker's cores."""
    backend = backend_name()
    deadline = time.time() + duration_s if duration_s else 0.0
    workers = max(1, round(intensity * len(cores))) if cores else max(1, round(intensity))
    core_list = ",".join(str(c) for c in cores)

    processes: list[subprocess.Popen] = []
    if backend == "stress-ng":
        args = ["stress-ng", "--cpu", str(workers), "--cpu-load", "100"]
        if core_list:
            args += ["--taskset", core_list]
        if duration_s:
            args += ["--timeout", f"{int(duration_s)}s"]
        processes.append(_spawn(args))
    else:
        for _ in range(workers):
            processes.append(_spawn([sys.executable, "-c", _CPU_CODE, core_list, str(deadline)]))

    return ExternalPressure(
        kind="cpu", target_worker=target_worker, cores=cores,
        intensity=intensity, backend=backend, processes=processes,
    )


def start_memory_pressure(
    target_worker: str, cores: tuple[int, ...], *, megabytes: float = 1024.0,
    duration_s: float | None = None,
) -> ExternalPressure:
    """Hold ``megabytes`` MB resident on the host, outside Nimbus."""
    backend = backend_name()
    deadline = time.time() + duration_s if duration_s else 0.0

    processes: list[subprocess.Popen] = []
    if backend == "stress-ng":
        args = ["stress-ng", "--vm", "1", "--vm-bytes", f"{int(megabytes)}M", "--vm-keep"]
        if duration_s:
            args += ["--timeout", f"{int(duration_s)}s"]
        processes.append(_spawn(args))
    else:
        processes.append(_spawn([sys.executable, "-c", _MEM_CODE, str(int(megabytes)), str(deadline)]))

    return ExternalPressure(
        kind="memory", target_worker=target_worker, cores=cores,
        intensity=megabytes, backend=backend, processes=processes,
    )


def start(spec, cores: tuple[int, ...]) -> ExternalPressure:
    """Start the stressor described by a ``PressureSpec``."""
    if spec.kind == "cpu":
        return start_cpu_pressure(
            spec.target_worker, cores, intensity=spec.intensity, duration_s=spec.duration_s
        )
    if spec.kind == "memory":
        return start_memory_pressure(
            spec.target_worker, cores, megabytes=spec.intensity, duration_s=spec.duration_s
        )
    raise ValueError(f"unknown pressure kind: {spec.kind!r}")
