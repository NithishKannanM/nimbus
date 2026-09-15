"""Worker registry: the control plane's view of the fleet.

Holds the three categories separately and hands out immutable snapshots:

    DECLARED  configured capacity        set at registration, never inferred
    RESERVED  Nimbus's own bookkeeping   updated on reserve/release
    OBSERVED  telemetry samples          optional; absent when telemetry is off

It is also the read cache the scheduler consults on the hot path, so a
placement never waits on a network round-trip -- otherwise the
scheduling-latency metric would measure the datastore rather than the scheduler.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field

from backend.models.enums import WorkerStatus
from backend.models.job import utcnow
from backend.models.telemetry import ObservedWindow, WorkerTelemetry
from backend.models.worker import (
    DeclaredCapacity,
    FleetSnapshot,
    ReservedResources,
    WorkerState,
)


@dataclass
class WorkerRecord:
    worker_id: str
    declared: DeclaredCapacity
    status: WorkerStatus = WorkerStatus.HEALTHY
    active_jobs: int = 0
    reserved_cpu_units: int = 0
    reserved_memory_mb: int = 0
    cores: tuple[int, ...] = ()
    last_heartbeat: float = field(default_factory=time.monotonic)
    telemetry: WorkerTelemetry | None = None
    telemetry_at: float | None = None
    registered_at: float = field(default_factory=time.monotonic)
    #: execution_id -> (cpu_units, memory_mb). Keyed so release is idempotent.
    reservations: dict[str, tuple[int, int]] = field(default_factory=dict)


class WorkerRegistry:
    def __init__(
        self,
        *,
        heartbeat_timeout_s: float = 15.0,
        eviction_timeout_s: float = 60.0,
        telemetry_history: int = 300,
        telemetry_window: int = 3,
        clock=time.monotonic,
    ) -> None:
        self._lock = threading.RLock()
        self._workers: dict[str, WorkerRecord] = {}
        self._history: dict[str, deque[WorkerTelemetry]] = {}
        self._heartbeat_timeout_s = heartbeat_timeout_s
        self._eviction_timeout_s = eviction_timeout_s
        self._telemetry_history = telemetry_history
        self._telemetry_window = telemetry_window
        self._clock = clock

    # -- membership -------------------------------------------------------
    def register(
        self,
        worker_id: str,
        *,
        slots: int,
        capacity_cpu_units: int,
        capacity_memory_mb: int,
        cores: tuple[int, ...] = (),
    ) -> WorkerRecord:
        with self._lock:
            record = WorkerRecord(
                worker_id=worker_id,
                declared=DeclaredCapacity(
                    cpu_units=capacity_cpu_units, memory_mb=capacity_memory_mb, slots=slots
                ),
                cores=cores,
                last_heartbeat=self._clock(),
            )
            self._workers[worker_id] = record
            self._history[worker_id] = deque(maxlen=self._telemetry_history)
            return record

    def worker_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._workers)

    def cores_for(self, worker_id: str) -> tuple[int, ...]:
        with self._lock:
            record = self._workers.get(worker_id)
            return record.cores if record else ()

    # -- liveness ---------------------------------------------------------
    def heartbeat(self, worker_id: str) -> None:
        """Liveness only. Deliberately carries no resource information: a
        worker must be able to prove it is alive with telemetry disabled."""
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                return
            record.last_heartbeat = self._clock()
            if record.status is WorkerStatus.UNHEALTHY:
                record.status = WorkerStatus.HEALTHY

    def set_status(self, worker_id: str, status: WorkerStatus) -> None:
        with self._lock:
            if worker_id in self._workers:
                self._workers[worker_id].status = status

    # -- observed ---------------------------------------------------------
    def ingest_telemetry(self, sample: WorkerTelemetry) -> None:
        """Record an observed sample.

        Telemetry also refreshes the heartbeat -- a worker reporting metrics is
        self-evidently alive -- but the reverse is not true, so heartbeats
        remain an independent mechanism.
        """
        with self._lock:
            record = self._workers.get(sample.worker_id)
            if record is None:
                return
            record.telemetry = sample
            record.telemetry_at = self._clock()
            record.last_heartbeat = self._clock()
            self._history[sample.worker_id].append(sample)

    def telemetry_history(self, worker_id: str, limit: int = 60) -> list[WorkerTelemetry]:
        with self._lock:
            history = self._history.get(worker_id)
            if not history:
                return []
            return list(history)[-limit:]

    def clear_telemetry(self) -> None:
        """Drop all observed state. Used by the telemetry ON/OFF control so the
        OFF arm genuinely has no observations, rather than stale ones."""
        with self._lock:
            for record in self._workers.values():
                record.telemetry = None
                record.telemetry_at = None
            for history in self._history.values():
                history.clear()

    # -- reserved ---------------------------------------------------------
    def reserve(
        self,
        worker_id: str,
        *,
        cpu_units: int,
        memory_mb: int,
        execution_id: str | None = None,
    ) -> None:
        """Commit capacity on a worker for one dispatch.

        Passing ``execution_id`` makes the matching release idempotent, which
        Phase 4 needs: a timed-out job is reclaimed by the reaper *and* may
        still push a late result when its process finally finishes. Both paths
        legitimately try to release, and without a key the second one would
        subtract a second time and leave the worker looking emptier than it is
        -- a bookkeeping error that feeds straight into every placement score.

        The anonymous form is kept for the demo scripts, which reserve
        synthetic load with no execution behind it.
        """
        with self._lock:
            record = self._workers[worker_id]
            if execution_id is not None:
                if execution_id in record.reservations:
                    return
                record.reservations[execution_id] = (cpu_units, memory_mb)
            record.active_jobs += 1
            record.reserved_cpu_units += cpu_units
            record.reserved_memory_mb += memory_mb

    def release(
        self,
        worker_id: str,
        *,
        cpu_units: int | None = None,
        memory_mb: int | None = None,
        execution_id: str | None = None,
    ) -> bool:
        """Give capacity back. Returns whether anything was actually released.

        With ``execution_id``, the amounts are taken from what was reserved
        rather than from the caller's arguments, so a release can never
        disagree with its reservation, and a repeat release is a no-op that
        returns False.
        """
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                return False
            if execution_id is not None:
                held = record.reservations.pop(execution_id, None)
                if held is None:
                    return False
                cpu_units, memory_mb = held
            record.active_jobs = max(0, record.active_jobs - 1)
            record.reserved_cpu_units = max(0, record.reserved_cpu_units - (cpu_units or 0))
            record.reserved_memory_mb = max(0, record.reserved_memory_mb - (memory_mb or 0))
            return True

    def reserved_executions(self, worker_id: str) -> tuple[str, ...]:
        """Execution ids currently holding capacity on this worker."""
        with self._lock:
            record = self._workers.get(worker_id)
            return tuple(record.reservations) if record else ()

    def release_all(self, worker_id: str) -> tuple[str, ...]:
        """Drop every keyed reservation on a worker and return their ids.

        Used when a worker is declared lost: its reservations describe work
        that will never report, and leaving them in place would keep a dead
        worker looking busy forever.
        """
        with self._lock:
            record = self._workers.get(worker_id)
            if record is None:
                return ()
            released = tuple(record.reservations)
            for execution_id in released:
                self.release(worker_id, execution_id=execution_id)
            return released

    def _observed_window(self, worker_id: str) -> ObservedWindow | None:
        """Summarise the last N samples for one worker.

        The registry owns the history buffer, so it computes the smoothing here
        rather than handing history to a placement policy -- policies stay pure
        functions of the snapshot they receive.
        """
        history = self._history.get(worker_id)
        if not history:
            return None
        window = list(history)[-self._telemetry_window :]
        values = [s.cpu_percent for s in window]
        times = [s.timestamp.timestamp() for s in window]
        span = times[-1] - times[0] if len(times) > 1 else 0.0

        # Least-squares slope of CPU% against seconds. Positive = rising.
        slope = 0.0
        if len(window) > 1 and span > 0:
            mean_t = sum(times) / len(times)
            mean_v = sum(values) / len(values)
            denominator = sum((t - mean_t) ** 2 for t in times)
            if denominator > 0:
                slope = sum(
                    (t - mean_t) * (v - mean_v) for t, v in zip(times, values)
                ) / denominator

        return ObservedWindow(
            cpu_percent_mean=round(sum(values) / len(values), 4),
            cpu_percent_latest=round(values[-1], 4),
            cpu_trend_pct_per_s=round(slope, 4),
            samples=len(window),
            span_s=round(span, 4),
        )

    # -- snapshots --------------------------------------------------------
    def _effective_status(self, record: WorkerRecord, now: float) -> WorkerStatus:
        """Health derived from heartbeat age. DRAINING is administrative and is
        never overridden by liveness."""
        if record.status is WorkerStatus.DRAINING:
            return WorkerStatus.DRAINING
        age = now - record.last_heartbeat
        if age > self._eviction_timeout_s:
            return WorkerStatus.DEAD
        if age > self._heartbeat_timeout_s:
            return WorkerStatus.UNHEALTHY
        return WorkerStatus.HEALTHY

    def snapshot(self) -> FleetSnapshot:
        with self._lock:
            now = self._clock()
            states = []
            for record in self._workers.values():
                status = self._effective_status(record, now)
                record.status = status
                states.append(
                    WorkerState(
                        worker_id=record.worker_id,
                        status=status,
                        declared=record.declared,
                        reserved=ReservedResources(
                            cpu_units=record.reserved_cpu_units,
                            memory_mb=record.reserved_memory_mb,
                            active_jobs=record.active_jobs,
                        ),
                        observed=record.telemetry,
                        observed_window=self._observed_window(record.worker_id),
                        heartbeat_age_s=now - record.last_heartbeat,
                        telemetry_age_s=(
                            None if record.telemetry_at is None else now - record.telemetry_at
                        ),
                    )
                )
            states.sort(key=lambda s: s.worker_id)
            return FleetSnapshot(taken_at=utcnow(), workers=tuple(states))

    def total_free_slots(self) -> int:
        return sum(s.free_slots for s in self.snapshot().healthy())
