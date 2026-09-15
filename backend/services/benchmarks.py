"""Benchmark orchestration behind the API.

A run takes minutes, so ``POST /benchmarks`` cannot be synchronous. This service
owns the background execution and the run registry.

**Runs are serialised, and that is a correctness requirement rather than a
resource-management convenience.** A benchmark run builds its own engine with
its own fleet of process pools pinned to disjoint core sets. Two runs at once
would pin two fleets to the *same* cores, so each would be measuring the other's
interference. Both results would be wrong, and neither would look wrong: they
would simply be slower, uniformly, in a way no metric distinguishes from a
scheduler being bad. A queue of one is the only honest option.

The API engine is deliberately not reused for runs either. A run needs a
specific strategy, a specific telemetry setting and a clean fleet; borrowing the
live engine would mean the run's numbers included whatever else the API was
being asked to do at the time.
"""

from __future__ import annotations

import logging
import threading
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any

log = logging.getLogger("nimbus.benchmarks")


class RunState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


@dataclass
class BenchmarkRequest:
    profile: str
    strategies: list[str]
    job_count: int = 60
    trials: int = 1
    seed: int = 1337
    telemetry: bool = True
    size_scale: float = 1.0
    worker_count: int = 3
    worker_slots: int = 2
    warmup_jobs: int = 3
    timeout_s: float = 600.0
    apply_pressure: bool = True

    def cells(self) -> list[tuple[int, str]]:
        """(trial, strategy) pairs in execution order.

        Strategies are interleaved *within* each trial rather than run in
        blocks. A machine that warms up, throttles or picks up background load
        over a long sweep would otherwise systematically favour whichever
        strategy ran during the good stretch -- and that bias is invisible in
        the results, because every strategy still produces plausible numbers.
        docs/experiments.md section 2.3 requires randomised/interleaved order
        for exactly this reason.
        """
        return [
            (trial, strategy)
            for trial in range(self.trials)
            for strategy in self.strategies
        ]


@dataclass
class BenchmarkRun:
    """One submitted sweep: a set of (trial, strategy) cells."""

    benchmark_id: str
    request: BenchmarkRequest
    state: RunState = RunState.QUEUED
    submitted_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    started_at: str | None = None
    finished_at: str | None = None
    cells_total: int = 0
    cells_done: int = 0
    run_ids: list[str] = field(default_factory=list)
    error: str | None = None
    cancel_requested: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "benchmark_id": self.benchmark_id,
            "state": self.state.value,
            "profile": self.request.profile,
            "strategies": list(self.request.strategies),
            "job_count": self.request.job_count,
            "trials": self.request.trials,
            "seed": self.request.seed,
            "telemetry": self.request.telemetry,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "cells_total": self.cells_total,
            "cells_done": self.cells_done,
            "progress": round(self.cells_done / self.cells_total, 3)
            if self.cells_total
            else 0.0,
            "run_ids": list(self.run_ids),
            "error": self.error,
        }


class BenchmarkService:
    def __init__(self, *, artifact_dir: str, history: int = 100) -> None:
        self._artifact_dir = artifact_dir
        self._lock = threading.RLock()
        self._runs: OrderedDict[str, BenchmarkRun] = OrderedDict()
        self._history = history
        # One worker thread, so runs execute strictly one at a time. See the
        # module docstring: concurrency here would not be a performance
        # trade-off, it would be a measurement error.
        self._thread: threading.Thread | None = None
        self._pending: list[str] = []
        self._wake = threading.Condition(self._lock)
        self._stopping = False

    # ------------------------------------------------------------------ #
    @property
    def artifact_dir(self) -> str:
        return self._artifact_dir

    def start(self) -> None:
        with self._lock:
            if self._thread is not None:
                return
            self._stopping = False
            self._thread = threading.Thread(
                target=self._worker, name="nimbus-benchmarks", daemon=True
            )
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        with self._wake:
            self._stopping = True
            self._wake.notify_all()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    # ------------------------------------------------------------------ #
    def submit(self, request: BenchmarkRequest) -> BenchmarkRun:
        from benchmark.profiles import PROFILES  # noqa: PLC0415
        from scheduler import STRATEGIES  # noqa: PLC0415

        if request.profile not in PROFILES:
            raise ValueError(
                f"unknown profile {request.profile!r}; "
                f"available: {', '.join(sorted(PROFILES))}"
            )
        unknown = [s for s in request.strategies if s not in STRATEGIES]
        if unknown:
            raise ValueError(
                f"unknown strategy/strategies: {', '.join(unknown)}; "
                f"available: {', '.join(sorted(STRATEGIES))}"
            )
        if not request.strategies:
            raise ValueError("at least one strategy is required")

        run = BenchmarkRun(
            benchmark_id=f"bench-{uuid.uuid4().hex[:8]}",
            request=request,
            cells_total=len(request.cells()),
        )
        with self._wake:
            self._runs[run.benchmark_id] = run
            self._pending.append(run.benchmark_id)
            self._trim()
            self._wake.notify_all()
        self.start()
        log.info("queued %s: %d cell(s)", run.benchmark_id, run.cells_total)
        return run

    def get(self, benchmark_id: str) -> BenchmarkRun | None:
        with self._lock:
            return self._runs.get(benchmark_id)

    def list(self, limit: int = 50) -> list[BenchmarkRun]:
        with self._lock:
            return list(reversed(list(self._runs.values())))[:limit]

    def cancel(self, benchmark_id: str) -> bool:
        """Request cancellation. Takes effect between cells, not within one.

        A cell is a complete measured run; interrupting one mid-flight would
        archive a truncated result, and a truncated run reports a perfectly
        ordinary makespan for a workload it did not finish. Stopping at a cell
        boundary means every archived run is whole.
        """
        with self._lock:
            run = self._runs.get(benchmark_id)
            if run is None or run.state in (
                RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED
            ):
                return False
            run.cancel_requested = True
            if run.state is RunState.QUEUED:
                run.state = RunState.CANCELLED
                run.finished_at = _now()
                if benchmark_id in self._pending:
                    self._pending.remove(benchmark_id)
            return True

    def _trim(self) -> None:
        while len(self._runs) > self._history:
            benchmark_id, run = next(iter(self._runs.items()))
            if run.state in (RunState.QUEUED, RunState.RUNNING):
                break  # never evict something still in flight
            self._runs.popitem(last=False)

    # ------------------------------------------------------------------ #
    def _worker(self) -> None:
        while True:
            with self._wake:
                while not self._pending and not self._stopping:
                    self._wake.wait(timeout=1.0)
                if self._stopping:
                    return
                benchmark_id = self._pending.pop(0)
                run = self._runs[benchmark_id]
                if run.cancel_requested:
                    run.state = RunState.CANCELLED
                    run.finished_at = _now()
                    continue
                run.state = RunState.RUNNING
                run.started_at = _now()
            self._execute(run)

    def _execute(self, run: BenchmarkRun) -> None:
        from benchmark.runner import run_experiment  # noqa: PLC0415

        request = run.request
        try:
            for trial, strategy in request.cells():
                if run.cancel_requested:
                    with self._lock:
                        run.state = RunState.CANCELLED
                        run.finished_at = _now()
                    log.info("%s cancelled after %d cell(s)", run.benchmark_id, run.cells_done)
                    return
                result = run_experiment(
                    profile=request.profile,
                    strategy=strategy,
                    job_count=request.job_count,
                    # Same seed across strategies on purpose: every arm runs a
                    # literally identical job sequence, so a difference between
                    # them is attributable to placement and not to the workload.
                    seed=request.seed,
                    telemetry=request.telemetry,
                    size_scale=request.size_scale,
                    worker_count=request.worker_count,
                    worker_slots=request.worker_slots,
                    warmup_jobs=request.warmup_jobs,
                    trial=trial,
                    timeout_s=request.timeout_s,
                    artifact_dir=self._artifact_dir,
                    apply_pressure=request.apply_pressure,
                )
                with self._lock:
                    run.run_ids.append(result.run_id)
                    run.cells_done += 1
                log.info(
                    "%s cell %d/%d done: %s trial=%d %s",
                    run.benchmark_id, run.cells_done, run.cells_total,
                    strategy, trial, result.metrics.headline(),
                )
            with self._lock:
                run.state = RunState.COMPLETED
                run.finished_at = _now()
        except Exception as exc:  # noqa: BLE001 -- the boundary is intentional
            log.exception("%s failed", run.benchmark_id)
            with self._lock:
                run.state = RunState.FAILED
                run.error = f"{type(exc).__name__}: {exc}"
                run.finished_at = _now()
                # Kept because the cells that did finish are still valid runs,
                # already archived, and still usable evidence.
                run.error += "\n" + traceback.format_exc(limit=5)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
