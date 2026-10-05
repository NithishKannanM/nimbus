"""Local worker agent -- the data plane.

The agent accepts dispatched jobs, executes them, and reports results. It never
chooses what to run: that is the control plane's job, and keeping the split
strict is what makes the scheduling comparison meaningful
(docs/architecture.md section 2).

In Phase 5 this is replaced by an HTTP agent on an EC2 instance exposing the
same accept/reject contract. The interface here is deliberately the one an
HTTP handler would have.

Phase 4 added three things, all of which an HTTP agent would also need:

- **Dedupe.** ``execution_id`` is the idempotency token from
  docs/architecture.md section 8. A repeat dispatch of an execution the agent
  has already finished returns the cached result rather than running it again.
  At-least-once delivery makes duplicates a *when*, not an *if*.
- **Abandonment.** The control plane can disown an execution it has given up
  on, freeing the slot and suppressing the eventual result.
- **Crash and recover.** A real, destructive worker failure, so the failure
  tests exercise detection and reclamation rather than a mock.
"""

from __future__ import annotations

import logging
import multiprocessing
import threading
from collections import OrderedDict
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Callable

from backend.models.job import Job
from worker.executor import (
    ExecutionResult,
    execute,
    force_single_threaded_math,
    pin_to_cores,
)

log = logging.getLogger("nimbus.worker")

CompletionSink = Callable[[ExecutionResult], None]

#: How many finished executions to remember for dedupe. Bounded because this is
#: a cache, not a record: the store holds the authoritative job history, and an
#: unbounded map here would grow with every job a long-lived worker ever ran.
DEDUPE_CACHE_SIZE = 256


class LocalWorkerAgent:
    def __init__(
        self,
        worker_id: str,
        *,
        slots: int = 2,
        cores: list[int] | None = None,
        scratch_dir: str | None = None,
        sink: CompletionSink,
        dedupe_cache_size: int = DEDUPE_CACHE_SIZE,
    ) -> None:
        self.worker_id = worker_id
        self.slots = slots
        self.cores = cores
        self._sink = sink
        self._lock = threading.Lock()
        self._active: dict[str, Job] = {}
        self._pool: ProcessPoolExecutor | None = None
        self._scratch = Path(scratch_dir or f".nimbus/scratch/{worker_id}")
        self._dedupe_cache_size = dedupe_cache_size
        #: execution_id -> the result it produced. LRU, bounded.
        self._finished: OrderedDict[str, ExecutionResult] = OrderedDict()
        #: Executions the control plane disowned. Their results are dropped.
        self._abandoned: set[str] = set()
        self._crashed = False
        #: Reported metrics, not log lines: docs/architecture.md section 8
        #: requires the duplicate rate to be measured rather than hidden.
        self.duplicate_dispatches = 0
        self.abandoned_executions = 0
        self.crashes = 0

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self._scratch.mkdir(parents=True, exist_ok=True)
        # Must happen before the forkserver process is spawned: it preloads
        # numpy, and BLAS reads its thread count at import time.
        force_single_threaded_math()
        ctx = multiprocessing.get_context("forkserver")
        # Preloading keeps numpy import cost out of the first job's measured
        # execution time.
        ctx.set_forkserver_preload(["worker.workloads.registry", "worker.executor"])
        self._pool = ProcessPoolExecutor(
            max_workers=self.slots,
            mp_context=ctx,
            initializer=pin_to_cores,
            initargs=(self.cores,),
        )
        self._crashed = False

    def stop(self, *, wait: bool = True) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=wait, cancel_futures=not wait)
            self._pool = None

    # -- dispatch contract -------------------------------------------------
    @property
    def active_jobs(self) -> int:
        with self._lock:
            return len(self._active)

    @property
    def crashed(self) -> bool:
        return self._crashed

    def alive(self) -> bool:
        """In-process workers are alive by construction; a simulated failure is
        tracked by the engine, not here. Remote clients actually probe."""
        return True

    def try_execute(self, job: Job) -> bool:
        """Accept or reject a dispatch. Returns False when at capacity --
        the HTTP equivalent of a 409, and a fast signal the scheduler can act
        on without a queue round-trip.

        A redelivery of an execution this agent already completed is accepted
        and answered from the dedupe cache. Rejecting it would be wrong: the
        control plane would re-place work that has in fact already run, which is
        the duplicate-execution failure the token exists to prevent.
        """
        if self._pool is None or self._crashed:
            return False

        with self._lock:
            cached = self._finished.get(job.execution_id)
            if cached is not None:
                self._finished.move_to_end(job.execution_id)
                self.duplicate_dispatches += 1
            elif job.execution_id in self._active:
                # Already running here. Also a duplicate, and also not a
                # rejection: the original execution will report for both.
                self.duplicate_dispatches += 1
                return True
            elif len(self._active) >= self.slots:
                return False
            else:
                cached = None
                self._active[job.execution_id] = job
                self._abandoned.discard(job.execution_id)

        if cached is not None:
            log.warning(
                "duplicate dispatch of %s on %s; replaying cached result",
                job.execution_id, self.worker_id,
            )
            # Replayed on the sink like any other completion so the control
            # plane's accounting is identical either way. Its store-side
            # conditional write is what discards it if the job has moved on.
            self._sink(cached)
            return True

        try:
            future = self._pool.submit(
                execute, job.job_type, job.payload, str(self._scratch)
            )
        except Exception:
            with self._lock:
                self._active.pop(job.execution_id, None)
            raise
        future.add_done_callback(lambda f: self._finish(job, f))
        return True

    def abandon(self, execution_id: str) -> bool:
        """Disown an execution: free its slot and drop its eventual result.

        Called when the control plane has already reclaimed the job (timeout,
        or the worker was declared lost). The underlying process is *not*
        killed -- see the limitation below -- so the result will still arrive,
        and this is what makes sure it is discarded rather than credited to a
        job that has since been re-placed elsewhere.

        **Known limitation.** ``ProcessPoolExecutor`` gives no way to terminate
        one in-flight task: killing its child breaks the pool for every other
        task on that worker. So a locally timed-out job keeps burning CPU until
        it finishes on its own, and Nimbus under-counts the load on that host
        for the remainder. On EC2 (Phase 5) the HTTP agent runs each job as its
        own process and can signal it. Recorded in docs/limitations.md section
        2 rather than papered over; ``abandoned_executions`` reports the rate.
        """
        with self._lock:
            job = self._active.pop(execution_id, None)
            if job is None:
                return False
            self._abandoned.add(execution_id)
            self.abandoned_executions += 1
        log.warning("%s abandoned %s; process runs on", self.worker_id, execution_id)
        return True

    def crash(self) -> tuple[str, ...]:
        """Destroy this worker's executor, losing every job on it.

        This is the real thing, not a flag: the pool is torn down with
        ``cancel_futures``, its children are terminated, and the in-flight jobs
        produce no result and never will. That is what makes the failure test
        meaningful -- the control plane has to *detect* the loss from the
        absence of heartbeats, exactly as it would for an EC2 instance that
        went away, rather than being told about it.

        Returns the execution ids that were lost.
        """
        with self._lock:
            lost = tuple(self._active)
            self._active.clear()
            self._abandoned.update(lost)
            self._crashed = True
            self.crashes += 1
            pool, self._pool = self._pool, None
        if pool is not None:
            # Snapshot the children first: shutdown() clears ``_processes``,
            # and its own termination is cooperative -- it waits for a running
            # task to finish rather than interrupting it. A crash that politely
            # waits for its victim is not a crash, so the kill has to come from
            # here, with handles taken before shutdown drops them.
            children = list((getattr(pool, "_processes", None) or {}).values())
            pool.shutdown(wait=False, cancel_futures=True)
            for process in children:
                try:
                    process.kill()
                except Exception:  # noqa: BLE001 -- already exited
                    pass
        log.error("%s crashed, losing %d execution(s)", self.worker_id, len(lost))
        return lost

    def recover(self) -> None:
        """Bring a crashed worker back with a fresh executor.

        The dedupe cache survives on purpose. A recovered worker that is handed
        a redelivery of something it completed before the crash must still
        answer from cache, or recovery would reintroduce the duplicate
        executions the token exists to prevent.
        """
        if not self._crashed:
            return
        self.start()
        log.info("%s recovered", self.worker_id)

    def child_pids(self) -> tuple[int, ...]:
        """PIDs of this worker's executor children.

        Reads ProcessPoolExecutor's internal process map: there is no public
        accessor, and per-worker attribution is exactly what telemetry needs --
        walking our own children would mix all three workers together.
        """
        pool = self._pool
        if pool is None:
            return ()
        try:
            return tuple(getattr(pool, "_processes", {}) or {})
        except Exception:
            return ()

    def stats(self) -> dict[str, int | bool]:
        with self._lock:
            return {
                "active_jobs": len(self._active),
                "slots": self.slots,
                "crashed": self._crashed,
                "duplicate_dispatches": self.duplicate_dispatches,
                "abandoned_executions": self.abandoned_executions,
                "crashes": self.crashes,
            }

    def _finish(self, job: Job, future: Future) -> None:
        with self._lock:
            self._active.pop(job.execution_id, None)
            abandoned = job.execution_id in self._abandoned
            self._abandoned.discard(job.execution_id)
        try:
            envelope = future.result()
        except Exception as exc:  # pool died, process killed, etc.
            envelope = {
                "ok": False,
                "result": None,
                "error": f"executor failure: {type(exc).__name__}: {exc}",
                "duration_ms": 0.0,
            }
        result = ExecutionResult(
            job_id=job.job_id,
            execution_id=job.execution_id,
            worker_id=self.worker_id,
            ok=bool(envelope["ok"]),
            result=envelope.get("result"),
            error=envelope.get("error"),
            execution_time_ms=float(envelope.get("duration_ms") or 0.0),
            cpu_units=job.cpu_units,
            memory_mb=job.memory_mb,
        )
        if abandoned:
            # The control plane already reclaimed this job and re-placed it.
            # Reporting now would race the new attempt for the same job record.
            log.info("dropping result for abandoned %s", job.execution_id)
            return
        with self._lock:
            self._remember(result)
        self._sink(result)

    def _remember(self, result: ExecutionResult) -> None:
        """Cache a finished execution for dedupe. Caller holds the lock."""
        self._finished[result.execution_id] = result
        self._finished.move_to_end(result.execution_id)
        while len(self._finished) > self._dedupe_cache_size:
            self._finished.popitem(last=False)
