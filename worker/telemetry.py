"""Worker telemetry collection via psutil.

Produces the OBSERVED half of a worker's state. Nothing here feeds placement:
samples go to the registry, into decision traces, and out through the API.
Whether they carry placement-relevant information is the question Phase 3 will
test, and answering it honestly requires that Phase 2's baseline cannot read
them.

Local scoping
-------------
A local worker is a process pool pinned to a disjoint core set, not its own EC2
instance, so "this worker's CPU" is the mean utilisation of *its cores*. That is
what makes per-worker CPU an independent signal locally, and it is what lets an
external stressor pinned to those cores show up against the right worker.

Memory has no equivalent local isolation: the host's RAM is shared. Both a
host-scoped ``memory_percent`` and a worker-scoped ``process_memory_mb`` are
recorded, labelled, and never conflated. On EC2 (Phase 5) the host-scoped
fields become genuinely per-worker.

Sole-caller rule
----------------
``psutil.cpu_percent(interval=None)`` reports utilisation since the *previous
call by this process*. Two callers would steal each other's deltas, so
``TelemetryCollector`` must be the only component that calls it.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Sequence

import psutil

from backend.models.job import utcnow
from backend.models.telemetry import WorkerTelemetry

#: worker_id -> pids of that worker's executor children
PidProvider = Callable[[str], Sequence[int]]
#: worker_id -> jobs currently executing on it
ActiveJobsProvider = Callable[[str], int]


@dataclass
class CollectorStats:
    """Telemetry overhead, measured rather than assumed."""

    samples: int = 0
    total_sample_ms: float = 0.0
    ticks: int = 0
    total_tick_ms: float = 0.0
    errors: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def mean_sample_ms(self) -> float:
        return self.total_sample_ms / self.samples if self.samples else 0.0

    @property
    def mean_tick_ms(self) -> float:
        return self.total_tick_ms / self.ticks if self.ticks else 0.0

    def duty_cycle(self, interval_s: float) -> float:
        """Fraction of wall time spent collecting telemetry."""
        if interval_s <= 0 or not self.ticks:
            return 0.0
        return (self.total_tick_ms / 1000.0) / (self.ticks * interval_s)

    def as_dict(self, interval_s: float) -> dict[str, float]:
        return {
            "samples": float(self.samples),
            "ticks": float(self.ticks),
            "errors": float(self.errors),
            "mean_sample_ms": round(self.mean_sample_ms, 4),
            "mean_tick_ms": round(self.mean_tick_ms, 4),
            "total_tick_ms": round(self.total_tick_ms, 3),
            "duty_cycle": round(self.duty_cycle(interval_s), 6),
        }


def load_average_1m() -> float:
    try:
        return os.getloadavg()[0]
    except (AttributeError, OSError):  # not available on every platform
        return 0.0


def process_memory_mb(pids: Sequence[int]) -> float:
    """Resident set size of a worker's executor children, in MB."""
    total = 0
    for pid in pids:
        try:
            total += psutil.Process(pid).memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied, ProcessLookupError):
            continue
    return total / 1e6


def sample_worker(
    worker_id: str, *, pids: Sequence[int] = (), active_jobs: int = 0
) -> WorkerTelemetry:
    """One host-scoped sample, for a worker that *is* its own host.

    The Phase 5 counterpart to ``TelemetryCollector.collect_once``. On EC2 a
    worker owns the whole instance, so there is no core-affinity scoping to do:
    ``cpu_percent`` for the machine is this worker's CPU, and ``memory_percent``
    is genuinely per-worker rather than shared.

    That difference is exactly why docs/limitations.md section 3 treats the
    local numbers as directional and requires the headline experiment to be
    re-run here. Locally, three workers share one machine's page cache and
    memory bandwidth and only their cores are disjoint; here they do not share
    anything.

    The sole-caller rule from the module docstring still applies: within one
    process, only one component may call ``psutil.cpu_percent(interval=None)``,
    or the two callers steal each other's deltas. In the worker agent that
    caller is this function.
    """
    per_core = psutil.cpu_percent(percpu=True, interval=None)
    memory = psutil.virtual_memory()
    started = time.perf_counter()
    return WorkerTelemetry(
        worker_id=worker_id,
        timestamp=utcnow(),
        cpu_percent=round(sum(per_core) / len(per_core), 2) if per_core else 0.0,
        cpu_percent_per_core=tuple(per_core),
        process_memory_mb=round(process_memory_mb(pids), 2),
        active_jobs=active_jobs,
        memory_percent=memory.percent,
        available_memory_mb=round(memory.available / 1e6, 1),
        load_average_1m=round(load_average_1m(), 2),
        sample_duration_ms=round((time.perf_counter() - started) * 1000.0, 4),
    )


class TelemetryCollector:
    """Samples every registered worker on a fixed interval.

    In Phase 5 this logic moves into the per-instance worker agent and reports
    over the network. Locally, one thread computes each worker's view from its
    core set, which is equivalent and materially cheaper.
    """

    def __init__(
        self,
        registry,
        *,
        interval_s: float = 2.0,
        pid_provider: PidProvider | None = None,
        active_jobs_provider: ActiveJobsProvider | None = None,
    ) -> None:
        self._registry = registry
        self._interval_s = interval_s
        self._pid_provider = pid_provider or (lambda _w: ())
        self._active_jobs_provider = active_jobs_provider or (lambda _w: 0)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.stats = CollectorStats()

    # -- lifecycle --------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        # Prime the per-core counters: the first cpu_percent call after import
        # reports utilisation since boot, which is not what we want.
        psutil.cpu_percent(percpu=True, interval=None)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="nimbus-telemetry", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- sampling ---------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            # Wait first: the primer call above needs an interval to elapse
            # before per-core deltas mean anything.
            if self._stop.wait(self._interval_s):
                break
            try:
                self.collect_once()
            except Exception:
                self.stats.errors += 1

    def collect_once(self) -> list[WorkerTelemetry]:
        """One sweep over the fleet. Exposed so tests can drive it directly."""
        tick_started = time.perf_counter()

        # Host-scoped reads: taken once per tick and shared by every worker,
        # because they describe the machine, not a worker.
        per_core = psutil.cpu_percent(percpu=True, interval=None)
        memory = psutil.virtual_memory()
        load1 = load_average_1m()
        taken_at = utcnow()

        samples: list[WorkerTelemetry] = []
        for worker_id in self._registry.worker_ids():
            sample_started = time.perf_counter()
            cores = self._registry.cores_for(worker_id)
            core_values = tuple(per_core[c] for c in cores if c < len(per_core))
            cpu_percent = sum(core_values) / len(core_values) if core_values else 0.0

            sample = WorkerTelemetry(
                worker_id=worker_id,
                timestamp=taken_at,
                cpu_percent=round(cpu_percent, 2),
                cpu_percent_per_core=core_values,
                process_memory_mb=round(process_memory_mb(self._pid_provider(worker_id)), 2),
                active_jobs=self._active_jobs_provider(worker_id),
                memory_percent=memory.percent,
                available_memory_mb=round(memory.available / 1e6, 1),
                load_average_1m=round(load1, 2),
                sample_duration_ms=round((time.perf_counter() - sample_started) * 1000.0, 4),
            )
            self._registry.ingest_telemetry(sample)
            samples.append(sample)
            self.stats.samples += 1
            self.stats.total_sample_ms += sample.sample_duration_ms

        self.stats.ticks += 1
        self.stats.total_tick_ms += (time.perf_counter() - tick_started) * 1000.0
        return samples
