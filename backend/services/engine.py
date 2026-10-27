"""NimbusEngine -- the control plane.

Owns the queue, the state store, the worker registry, the fleet and the
scheduler loop. Everything the API does, it does through here.

The loop is deliberately single-threaded and centralised: a single decision
point is what makes scheduling strategies comparable to each other. It is also
a throughput ceiling and a single point of failure, which is recorded in
docs/limitations.md section 2 rather than hidden.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Callable

import httpx

from backend.config import NimbusConfig, get_config
from backend.models.decision import SchedulingDecision
from backend.models.enums import TERMINAL_STATUSES, JobEvent, JobStatus, WorkerStatus
from backend.models.job import IllegalTransition, Job, utcnow
from backend.models.worker import FleetSnapshot
from backend.services.comparison import StrategySegment, compare_strategies
from backend.services.factory import (
    build_artifacts,
    build_metrics,
    build_queue,
    build_store,
    build_telemetry_collector,
    build_workers,
    validate,
)
from backend.services.metrics import Metric
from backend.services.queue.base import Lease, LeaseExpired, QueueDepth
from backend.services.reaper import Reaper, ReaperReport, Reclamation
from backend.services.registry import WorkerRegistry
from backend.services.retry import BackoffPolicy
from backend.services.store.base import ConditionFailed
from scheduler import build_scheduler
from worker.executor import ExecutionResult
from worker.heartbeat import HeartbeatEmitter
from worker.workloads import registry as workloads

log = logging.getLogger("nimbus.engine")


class NimbusEngine:
    def __init__(
        self,
        config: NimbusConfig | None = None,
        *,
        http_transport: Callable[[str], httpx.BaseTransport] | None = None,
    ) -> None:
        self.config = config or get_config()
        cfg = self.config
        #: test seam: worker_id -> httpx transport for worker_transport="http".
        self._http_transport = http_transport

        # Queue, store and artifacts come from the factory, which is the only
        # place in the codebase that branches on NIMBUS_BACKEND. Nothing below
        # this line knows or cares whether it is talking to a dict or to SQS
        # (docs/architecture.md section 6).
        problems = validate(cfg)
        if problems:
            raise ValueError("; ".join(problems))
        self.queue = build_queue(cfg)
        self.store = build_store(cfg)
        self.artifacts = build_artifacts(cfg)
        # Channel 1 (docs/architecture.md section 10). Write-only from here:
        # nothing in the scheduler reads these back.
        self.metrics = build_metrics(cfg)
        self.registry = WorkerRegistry(
            heartbeat_timeout_s=cfg.heartbeat_timeout_s,
            eviction_timeout_s=cfg.eviction_timeout_s,
            telemetry_history=cfg.telemetry_history,
            telemetry_window=cfg.telemetry_window,
        )
        self.scheduler = build_scheduler(
            cfg.strategy,
            aging_interval_s=cfg.aging_interval_s,
            telemetry_enabled=cfg.telemetry_enabled,
            telemetry_max_age_s=cfg.telemetry_max_age_s,
        )

        # OBSERVED state. Collected, stored and exposed -- and, for every
        # strategy implemented so far, structurally unreachable from
        # placement (scheduler/views.py).
        self.heartbeats = HeartbeatEmitter(
            self.registry,
            interval_s=cfg.heartbeat_interval_s,
            # A simulated worker failure has to look like a real one: the
            # worker stops beating and the control plane infers the loss. If
            # the engine simply marked it UNHEALTHY, the failure tests would be
            # exercising a setter rather than the detection path.
            # Remote workers are additionally probed over HTTP.
            is_alive=lambda worker_id: (
                worker_id not in self._failed_workers and self._worker_alive(worker_id)
            ),
        )
        self.telemetry = build_telemetry_collector(
            cfg,
            self.registry,
            workers=lambda: self._workers,
            pid_provider=self._worker_pids,
            active_jobs_provider=self._worker_active_jobs,
            is_alive=lambda worker_id: worker_id not in self._failed_workers,
        )
        self._telemetry_enabled = cfg.telemetry_enabled
        #: When each strategy was active, for the live comparison
        #: (backend/services/comparison.py). Process-local and unbounded in
        #: count only by how often someone clicks the switch.
        self._segments: list[StrategySegment] = [
            StrategySegment(self.scheduler.name, self.scheduler.placement.name, utcnow())
        ]

        self.backoff = BackoffPolicy(
            base_s=cfg.retry_base_s,
            cap_s=cfg.retry_cap_s,
            jitter_ratio=cfg.retry_jitter_ratio,
        )
        self.reaper = Reaper(
            store=self.store,
            queue=self.queue,
            registry=self.registry,
            reclaim=self.reclaim,
            has_lease=self.has_lease,
            default_timeout_s=cfg.job_timeout_s,
            orphan_grace_s=cfg.orphan_grace_s,
        )
        self._reaper_interval_s = cfg.reaper_interval_s
        self._last_sweep = 0.0
        self._metrics_sample_interval_s = cfg.metrics_sample_interval_s
        self._last_gauge_sample = 0.0
        self._failed_workers: set[str] = set()

        self._workers: dict[str, Any] = {}
        self._leases: dict[str, Lease] = {}
        self._lease_extended_at: dict[str, float] = {}
        self._completions: Queue[ExecutionResult] = Queue()
        self._loop_thread: threading.Thread | None = None
        self._running = threading.Event()
        self._lock = threading.RLock()
        self._stats: dict[str, int] = {
            "submitted": 0, "completed": 0, "failed": 0,
            "dispatch_rejected": 0, "no_candidate": 0,
            # Phase 4 reliability counters. Every one of these is a number the
            # report has to be able to quote: a retry rate hidden inside a log
            # file is indistinguishable from no retries at all.
            "retried": 0, "timed_out": 0, "worker_lost": 0,
            "dead_lettered": 0, "exhausted": 0, "stale_results": 0,
        }

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self._running.is_set():
            return
        self._start_fleet()
        self.metrics.start()
        # Heartbeats are unconditional: with telemetry off, workers must still
        # be able to prove liveness or the whole fleet drifts to UNHEALTHY.
        self.heartbeats.start()
        if self._telemetry_enabled:
            self.telemetry.start()
        self._running.set()
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="nimbus-scheduler", daemon=True
        )
        self._loop_thread.start()
        log.info("engine started: strategy=%s workers=%d", self.scheduler.name, len(self._workers))

    def stop(self, *, wait: bool = True) -> None:
        self._running.clear()
        self.telemetry.stop()
        self.heartbeats.stop()
        if self._loop_thread is not None:
            self._loop_thread.join(timeout=5.0)
            self._loop_thread = None
        for agent in self._workers.values():
            agent.stop(wait=wait)
        self._workers.clear()
        self.metrics.stop()  # final flush

    def _start_fleet(self) -> None:
        cfg = self.config
        # Local process pools or HTTP clients to remote hosts: the factory
        # decides, and everything below treats them identically.
        fleet = build_workers(
            cfg,
            sink=self._completions.put,
            cores_for=self._cores_for,
            reservations=lambda wid: len(self.registry.reserved_executions(wid)),
            transport_for=self._http_transport,
        )
        for worker_id, agent in fleet.items():
            agent.start()
            self._workers[worker_id] = agent
            self.registry.register(
                worker_id,
                slots=agent.slots,
                capacity_cpu_units=cfg.worker_cpu_units,
                capacity_memory_mb=cfg.worker_memory_mb,
                # Telemetry scopes a worker's CPU to its own cores, which is
                # what makes per-worker CPU an independent local signal.
                cores=tuple(agent.cores or ()),
            )

    def _worker_alive(self, worker_id: str) -> bool:
        """Liveness the heartbeat emitter acts on. In-process agents are always
        alive; a remote client probes its host's /health."""
        agent = self._workers.get(worker_id)
        return agent is None or agent.alive()

    def _cores_for(self, index: int) -> list[int] | None:
        """Disjoint core set per worker, so local workers approximate
        independent machines (docs/limitations.md section 3)."""
        try:
            available = sorted(os.sched_getaffinity(0))
        except AttributeError:
            return None
        per_worker = max(1, len(available) // max(1, self.config.worker_count))
        chunk = available[index * per_worker : (index + 1) * per_worker]
        return chunk or None

    # ------------------------------------------------------------------ #
    # submission (control plane entry point)
    # ------------------------------------------------------------------ #
    def submit(
        self,
        *,
        job_type: str,
        payload: dict[str, Any] | None = None,
        priority: int = 3,
        timeout_s: float | None = None,
        run_id: str | None = None,
    ) -> Job:
        payload = payload or {}
        spec, parsed = workloads.validate(job_type, payload)
        cpu_units, memory_mb = spec.requirements(parsed)
        job = Job(
            job_type=job_type,
            workload_class=spec.workload_class,
            priority=priority,
            payload=parsed.model_dump(),
            cpu_units=cpu_units,
            memory_mb=memory_mb,
            max_attempts=self.config.max_attempts,
            timeout_s=timeout_s or self.config.job_timeout_s,
            run_id=run_id,
        )
        self.store.put_job(job)
        self.queue.enqueue(job.job_id)
        with self._lock:
            self._stats["submitted"] += 1
        self.metrics.emit(Metric.JOBS_SUBMITTED, WorkloadClass=job.workload_class.value)
        return job

    # ------------------------------------------------------------------ #
    # scheduler loop
    # ------------------------------------------------------------------ #
    def _run_loop(self) -> None:
        tick = self.config.scheduler_tick_s
        while self._running.is_set():
            try:
                self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            time.sleep(tick)

    def tick(self) -> None:
        """One scheduling iteration. Exposed so tests can drive the loop
        deterministically instead of racing a background thread."""
        self._drain_completions()
        self._extend_leases()
        self._maybe_sweep()
        self._maybe_sample_gauges()
        self._dispatch_round()

    def _maybe_sweep(self) -> None:
        """Run the reaper on its own, slower cadence.

        The scheduler ticks every 20 ms; sweeping that often would scan the
        whole live job set fifty times a second to find nothing. The reaper's
        job is to bound detection latency, not to be fast, so it runs on
        ``reaper_interval_s`` and the scheduler loop just carries it.
        """
        now = time.monotonic()
        if now - self._last_sweep < self._reaper_interval_s:
            return
        self._last_sweep = now
        try:
            self.reaper.sweep()
        except Exception:
            log.exception("reaper sweep failed")

    def _maybe_sample_gauges(self) -> None:
        """Sample queue depth and worker health on ``metrics_sample_interval_s``.

        Gauges are sampled, not event-driven, so they get their own cadence
        rather than riding every 20 ms tick.
        """
        now = time.monotonic()
        if now - self._last_gauge_sample < self._metrics_sample_interval_s:
            return
        self._last_gauge_sample = now
        depth = self.queue.depth()
        self.metrics.emit(Metric.QUEUE_DEPTH_VISIBLE, depth.visible)
        self.metrics.emit(Metric.QUEUE_DEPTH_IN_FLIGHT, depth.in_flight)
        for worker in self.registry.snapshot().workers:
            self.metrics.emit(
                Metric.WORKER_HEALTHY,
                1.0 if worker.status is WorkerStatus.HEALTHY else 0.0,
                WorkerId=worker.worker_id,
            )

    def _dispatch_round(self) -> None:
        snapshot = self.registry.snapshot()
        free = sum(w.free_slots for w in snapshot.healthy())
        if free <= 0:
            return

        # Free slots bound how many jobs we could place, but only loosely: a
        # job's declared cpu_units/memory_mb can exhaust a worker before its
        # slots are full. Whatever we receive and cannot place goes down the
        # deferral path (_defer), which does not consume the retry budget.
        leases = self.queue.receive(max_messages=free)
        if not leases:
            return

        pending: dict[str, Lease] = {}
        jobs: list[Job] = []
        for lease in leases:
            job = self.store.get_job(lease.job_id)
            if job is None or job.status is not JobStatus.QUEUED:
                # Orphan or already advanced by another path: drop the message.
                self._safe_ack(lease)
                continue
            pending[job.job_id] = lease
            jobs.append(job)

        now = utcnow()
        while jobs:
            job = self.scheduler.select_next(jobs, now)
            if job is None:
                break
            jobs.remove(job)
            self._place_and_dispatch(job, pending[job.job_id], now)

    def _place_and_dispatch(self, job: Job, lease: Lease, now: datetime) -> None:
        snapshot = self.registry.snapshot()
        decision = self.scheduler.place(job, snapshot, now)
        self.store.put_decision(decision)
        strategy = self.scheduler.name
        self.metrics.emit(
            Metric.SCHEDULING_LATENCY_MS, decision.decision_latency_us / 1000.0, Strategy=strategy
        )

        if decision.selected_worker is None:
            with self._lock:
                self._stats["no_candidate"] += 1
            # Back off without a nack: a nack would make the message instantly
            # visible and spin the loop.
            self._defer(lease)
            return

        worker_id = decision.selected_worker
        scheduled = job.apply(JobEvent.SCHEDULE, at=now).model_copy(
            update={"assigned_worker": worker_id, "attempts": job.attempts + 1}
        )
        try:
            self.store.update_job(scheduled, expect_status=JobStatus.QUEUED)
        except ConditionFailed:
            log.warning("lost race scheduling %s; releasing", job.job_id)
            self._defer(lease)
            return

        # Keyed on the execution, so a reaper reclaim and a late result cannot
        # both subtract the same reservation (registry.reserve docstring).
        self.registry.reserve(
            worker_id,
            cpu_units=job.cpu_units,
            memory_mb=job.memory_mb,
            execution_id=scheduled.execution_id,
        )
        agent = self._workers[worker_id]
        accepted = False
        try:
            accepted = agent.try_execute(scheduled)
        except Exception:
            log.exception("dispatch to %s raised", worker_id)

        if not accepted:
            self.registry.release(worker_id, execution_id=scheduled.execution_id)
            # Roll the attempt back. Nothing ran: the worker was full and said
            # so before executing anything. Charging a retry for a rejection
            # would let a burst of "no room right now" exhaust a healthy job's
            # budget and fail it -- the same mistake the deferral path was
            # fixed for in Phase 3, arriving via Job.attempts instead of via
            # the queue's delivery count.
            requeued = scheduled.apply(JobEvent.DISPATCH_REJECTED, at=utcnow()).model_copy(
                update={"attempts": job.attempts}
            )
            self.store.update_job(requeued, expect_status=JobStatus.SCHEDULED)
            with self._lock:
                self._stats["dispatch_rejected"] += 1
            self._defer(lease)
            return

        running = scheduled.apply(JobEvent.ACCEPTED, at=utcnow())
        self.store.update_job(running, expect_status=JobStatus.SCHEDULED)
        if running.attempts == 1 and running.scheduled_at is not None:
            # First attempt only: on a retry created_at would fold earlier
            # attempts' run time into "waiting in the queue".
            self.metrics.emit(
                Metric.QUEUE_WAIT_MS,
                (running.scheduled_at - running.created_at).total_seconds() * 1000.0,
                Strategy=strategy,
                WorkloadClass=job.workload_class.value,
            )
        with self._lock:
            self._leases[job.job_id] = lease
            self._lease_extended_at[job.job_id] = time.monotonic()

    # ------------------------------------------------------------------ #
    # completion handling
    # ------------------------------------------------------------------ #
    def report_result(self, result: ExecutionResult) -> None:
        """Accept a completion reported from outside this process.

        The entry point for remote workers (worker/api.py), which POST their
        results rather than pushing them onto an in-process queue. It goes onto
        the same queue the local agents use and through the same handler, so a
        remote completion is subject to every guard a local one is -- the
        execution-id check, the conditional write, the idempotent reservation
        release. A second code path here would be a second place for the
        exactly-once guarantee to be got wrong.
        """
        self._completions.put(result)

    def _drain_completions(self) -> None:
        while True:
            try:
                result = self._completions.get_nowait()
            except Empty:
                return
            try:
                self._handle_completion(result)
            except Exception:
                log.exception("failed to handle completion for %s", result.job_id)

    def _handle_completion(self, result: ExecutionResult) -> None:
        # Idempotent in the execution, so a reaper reclaim that already gave
        # this capacity back does not have it taken twice.
        self.registry.release(result.worker_id, execution_id=result.execution_id)
        job = self.store.get_job(result.job_id)
        if job is None:
            log.warning("completion for unknown job %s", result.job_id)
            return

        # docs/architecture.md section 8, guard 3. A result is only allowed to
        # settle the attempt it belongs to. Comparing execution_id and not just
        # status is what stops a slow first attempt from overwriting the record
        # of the retry that has already been dispatched in its place -- both
        # are RUNNING, so status alone would let it through.
        if job.status is not JobStatus.RUNNING or job.execution_id != result.execution_id:
            with self._lock:
                self._stats["stale_results"] += 1
            self.metrics.emit(Metric.DUPLICATE_EXECUTIONS, Strategy=self.scheduler.name)
            log.warning(
                "discarding %s result for %s (job is %s at %s)",
                result.execution_id, result.job_id, job.status, job.execution_id,
            )
            return

        if result.ok:
            done = job.apply(JobEvent.COMPLETED).model_copy(
                update={"result": result.result, "execution_time_ms": result.execution_time_ms}
            )
            try:
                self.store.update_job(done, expect_status=JobStatus.RUNNING)
            except ConditionFailed:
                with self._lock:
                    self._stats["stale_results"] += 1
                self.metrics.emit(Metric.DUPLICATE_EXECUTIONS, Strategy=self.scheduler.name)
                return
            with self._lock:
                self._stats["completed"] += 1
            dims = {"Strategy": self.scheduler.name, "WorkloadClass": job.workload_class.value}
            self.metrics.emit(Metric.JOBS_COMPLETED, **dims)
            self.metrics.emit(Metric.EXECUTION_TIME_MS, result.execution_time_ms, **dims)
            self._release_lease(result.job_id, ack=True)
            return

        # A reported execution failure. Same resolution path as a reaped
        # timeout or a lost worker, because the question is identical from
        # here: is there budget left for another attempt?
        self._resolve_failure(
            job, event=JobEvent.FAILED, reason=result.error or "execution failed"
        )

    # ------------------------------------------------------------------ #
    # retry, reclamation and the DLQ
    # ------------------------------------------------------------------ #
    def _resolve_failure(
        self, job: Job, *, event: JobEvent, reason: str
    ) -> bool:
        """Advance one failed attempt to its next state: retry, or give up.

        Every failure in Nimbus converges here -- a workload that raised, a
        wall-clock timeout, a worker that vanished, a poison message. They
        differ only in the *event* that records the cause; the decision about
        what happens next is one policy in one place, and the transition table
        in ``backend/models/job.py`` is what validates each cause against the
        state it arrived in.

        Returns whether this call is the one that moved the job. A False means
        somebody else got there first, which is normal: the reaper and a late
        result can both be looking at the same doomed job.
        """
        origin = job.status
        try:
            interim = job.apply(event, at=utcnow()).model_copy(update={"error": reason})
        except IllegalTransition:
            log.warning("cannot apply %s to %s in %s", event, job.job_id, origin)
            return False
        try:
            self.store.update_job(interim, expect_status=origin)
        except ConditionFailed:
            # Lost the race. Whoever won is now responsible for the job.
            return False

        with self._lock:
            if event is JobEvent.TIMEOUT:
                self._stats["timed_out"] += 1
            elif event is JobEvent.WORKER_LOST:
                self._stats["worker_lost"] += 1

        if interim.status in TERMINAL_STATUSES:
            # DEAD_LETTER lands here: terminal on arrival, no retry decision to
            # make. The message is already out of the queue, so there is
            # nothing left to ack.
            with self._lock:
                self._stats["dead_lettered"] += 1
                self._stats["failed"] += 1
            self._forget_lease(interim.job_id)
            self._emit_job_metric(Metric.JOBS_FAILED, interim)
            log.warning("%s dead-lettered: %s", interim.job_id, reason)
            return True

        if interim.attempts >= interim.max_attempts:
            final = interim.apply(JobEvent.EXHAUSTED, at=utcnow())
            try:
                self.store.update_job(final, expect_status=interim.status)
            except ConditionFailed:
                return False
            with self._lock:
                self._stats["exhausted"] += 1
                self._stats["failed"] += 1
            self._emit_job_metric(Metric.JOBS_FAILED, final)
            self._release_lease(final.job_id, ack=True)
            log.warning(
                "%s failed after %d/%d attempts: %s",
                final.job_id, final.attempts, final.max_attempts, reason,
            )
            return True

        requeued = interim.apply(JobEvent.REQUEUE, at=utcnow())
        try:
            self.store.update_job(requeued, expect_status=interim.status)
        except ConditionFailed:
            return False
        delay = self.backoff.delay_for(requeued.job_id, requeued.attempts)
        self._return_to_queue(requeued.job_id, delay)
        with self._lock:
            self._stats["retried"] += 1
        self._emit_job_metric(Metric.JOBS_RETRIED, requeued)
        log.info(
            "%s retrying (attempt %d/%d) in %.2fs: %s",
            requeued.job_id, requeued.attempts + 1, requeued.max_attempts, delay, reason,
        )
        return True

    def _emit_job_metric(self, metric: Metric, job: Job) -> None:
        self.metrics.emit(
            metric, Strategy=self.scheduler.name, WorkloadClass=job.workload_class.value
        )

    def _return_to_queue(self, job_id: str, delay_s: float) -> None:
        """Make a job visible again after backoff.

        Uses the held lease when there is one, because ``release`` preserves
        the delivery count and that count is the poison-message backstop. An
        orphan has no lease to release -- that is what made it an orphan -- so
        it is re-sent instead, which is the only option and is why the DLQ
        threshold sits above the retry budget rather than at it.
        """
        with self._lock:
            lease = self._leases.pop(job_id, None)
            self._lease_extended_at.pop(job_id, None)
        if lease is not None:
            try:
                self.queue.release(lease, delay_s)
                return
            except LeaseExpired:
                log.warning("lease for %s expired before requeue", job_id)
        try:
            self.queue.enqueue(job_id, delay_s=delay_s)
        except ValueError:
            # The message is still in the queue -- in flight under a receipt
            # this process no longer holds, which is precisely what made the
            # job an orphan. Nothing can be done about it from here: SQS has no
            # way to delete or make visible a message without its receipt.
            # The visibility timeout is therefore the recovery bound for an
            # orphaned job, and that is a reason to keep it short rather than a
            # reason to work around it.
            log.info(
                "%s is in flight under a lost receipt; redelivery in <= %.0fs",
                job_id, self.config.visibility_timeout_s,
            )

    def reclaim(self, claim: Reclamation) -> bool:
        """Effect handler for the reaper. See backend/services/reaper.py.

        The reaper detects against a snapshot it took at the start of its
        sweep, so the first thing to do is re-read: by the time a claim is
        acted on the job may have completed, been retried, or been reclaimed by
        an earlier claim in the same sweep. Acting on the stale copy would
        re-place work that is already running somewhere.
        """
        job = self.store.get_job(claim.job.job_id)
        if job is None or job.status in TERMINAL_STATUSES:
            return False
        if job.status is not claim.job.status or job.attempts != claim.job.attempts:
            return False

        worker_id = job.assigned_worker
        if worker_id is not None:
            self.registry.release(worker_id, execution_id=job.execution_id)
            agent = self._workers.get(worker_id)
            if agent is not None:
                # Free the slot and make sure the result, if one ever arrives,
                # is not credited to the attempt we are about to replace.
                agent.abandon(job.execution_id)
        return self._resolve_failure(job, event=claim.event, reason=claim.reason)

    def has_lease(self, job_id: str) -> bool:
        with self._lock:
            return job_id in self._leases

    def sweep(self) -> ReaperReport:
        """Force one reaper pass. Exposed so failure tests can drive detection
        deterministically rather than sleeping through an interval."""
        self._last_sweep = time.monotonic()
        return self.reaper.sweep()

    # ------------------------------------------------------------------ #
    # lease plumbing
    # ------------------------------------------------------------------ #
    def _extend_leases(self) -> None:
        """Renew leases for running jobs. Letting a lease lapse mid-execution
        would redeliver the job and cause a duplicate run."""
        interval = self.config.visibility_timeout_s / 3.0
        now = time.monotonic()
        with self._lock:
            due = [
                job_id
                for job_id, last in self._lease_extended_at.items()
                if now - last >= interval
            ]
            leases = {job_id: self._leases.get(job_id) for job_id in due}
        for job_id, lease in leases.items():
            if lease is None:
                continue
            try:
                self.queue.extend(lease, self.config.visibility_timeout_s)
                with self._lock:
                    self._lease_extended_at[job_id] = now
            except LeaseExpired:
                log.warning("lease for %s expired before renewal", job_id)
                with self._lock:
                    self._leases.pop(job_id, None)
                    self._lease_extended_at.pop(job_id, None)

    def _release_lease(self, job_id: str, *, ack: bool) -> None:
        with self._lock:
            lease = self._leases.pop(job_id, None)
            self._lease_extended_at.pop(job_id, None)
        if lease is None:
            return
        try:
            self.queue.ack(lease) if ack else self.queue.nack(lease)
        except LeaseExpired:
            log.warning("lease for %s already gone", job_id)

    def _forget_lease(self, job_id: str) -> None:
        """Drop lease bookkeeping without touching the queue.

        For a dead-lettered job the message has already been moved out of the
        queue by redrive, so any ack would raise on a receipt that no longer
        refers to anything.
        """
        with self._lock:
            self._leases.pop(job_id, None)
            self._lease_extended_at.pop(job_id, None)

    def _safe_ack(self, lease: Lease) -> None:
        try:
            self.queue.ack(lease)
        except LeaseExpired:
            pass

    def _defer(self, lease: Lease, seconds: float = 0.05) -> None:
        """Return a job to the queue without consuming its retry budget.

        Delegates to the queue's atomic deferral. Doing this as ack-then-enqueue
        left a window in which the job was in neither the queue nor a worker,
        and a concurrent drain check could mistake that for "finished".
        """
        try:
            self.queue.defer(lease, seconds)
        except LeaseExpired:
            pass

    # ------------------------------------------------------------------ #
    # introspection (API surface)
    # ------------------------------------------------------------------ #
    def fleet(self) -> FleetSnapshot:
        return self.registry.snapshot()

    def queue_depth(self) -> QueueDepth:
        return self.queue.depth()

    def stats(self) -> dict[str, Any]:
        with self._lock:
            stats = dict(self._stats)
        depth = self.queue.depth()
        return {
            **stats,
            "strategy": self.scheduler.name,
            "ordering_policy": self.scheduler.ordering.name,
            "placement_policy": self.scheduler.placement.name,
            "algorithm_version": self.scheduler.placement.algorithm_version,
            "telemetry_enabled": self._telemetry_enabled,
            "telemetry_overhead": self.telemetry_overhead(),
            "queue_visible": depth.visible,
            "queue_in_flight": depth.in_flight,
            "dead_letters": len(self.queue.dead_letters()),
            "status_counts": self.store.counts_by_status(),
        }

    def decisions(self, *, job_id: str | None = None, limit: int = 50) -> list[SchedulingDecision]:
        return self.store.list_decisions(job_id=job_id, limit=limit)

    def comparison(self) -> dict[str, Any]:
        """Per-strategy live figures since this control plane started.
        Uncontrolled by construction; see backend/services/comparison.py."""
        with self._lock:
            segments = list(self._segments)
        return compare_strategies(
            self.store.list_decisions(limit=5000),
            self.store.list_jobs(status=JobStatus.COMPLETED, limit=5000),
            segments,
            utcnow(),
        )

    def wait_for_idle(self, timeout_s: float = 60.0, poll_s: float = 0.02) -> bool:
        """Block until the queue is drained and no worker is busy.

        Checks the *store* as well as the queue. Queue depth alone is not
        sufficient evidence that a run has finished: a job in transit between
        control-plane steps is briefly absent from the queue while still very
        much unfinished, and treating that as idle silently truncates benchmark
        runs.
        """
        deadline = time.monotonic() + timeout_s
        pending_states = (
            JobStatus.QUEUED, JobStatus.SCHEDULED, JobStatus.RUNNING,
            JobStatus.RETRYING, JobStatus.TIMED_OUT,
        )
        while time.monotonic() < deadline:
            depth = self.queue.depth()
            busy = any(a.active_jobs for a in self._workers.values())
            counts = self.store.counts_by_status()
            unfinished = sum(counts[s.value] for s in pending_states)
            if (
                depth.total == 0
                and not busy
                and self._completions.empty()
                and unfinished == 0
            ):
                return True
            time.sleep(poll_s)
        return False

    # ------------------------------------------------------------------ #
    # telemetry providers and the ON/OFF control
    # ------------------------------------------------------------------ #
    def _worker_pids(self, worker_id: str) -> tuple[int, ...]:
        agent = self._workers.get(worker_id)
        return agent.child_pids() if agent else ()

    def _worker_active_jobs(self, worker_id: str) -> int:
        agent = self._workers.get(worker_id)
        return agent.active_jobs if agent else 0

    @property
    def telemetry_enabled(self) -> bool:
        return self._telemetry_enabled

    def set_telemetry(self, enabled: bool) -> bool:
        """Toggle observed-state collection.

        Introduced as the Phase 2 control experiment, when no policy could read
        telemetry and flipping this had to leave placement bit-identical. From
        Phase 3, ``adaptive`` *does* consume telemetry, so disabling collection
        makes every worker look stale to it and adaptive-v1 falls back to
        reservation-derived estimates (docs/scheduling.md section 7.4). The
        static policies remain unaffected either way.
        """
        with self._lock:
            if enabled == self._telemetry_enabled:
                return self._telemetry_enabled
            self._telemetry_enabled = enabled
            if enabled:
                self.telemetry.start()
            else:
                self.telemetry.stop()
                # Drop observations so the OFF arm has none rather than stale
                # ones; a stale sample in a trace would be misleading.
                self.registry.clear_telemetry()
            self.scheduler.telemetry_enabled = enabled
        log.info("telemetry %s", "enabled" if enabled else "disabled")
        return self._telemetry_enabled

    def telemetry_overhead(self) -> dict[str, float]:
        return self.telemetry.stats.as_dict(self.config.telemetry_interval_s)

    # ------------------------------------------------------------------ #
    # failure simulation (admin surface, docs/architecture.md section 5)
    # ------------------------------------------------------------------ #
    def fail_worker(self, worker_id: str, *, hard: bool = True) -> dict[str, Any]:
        """Make a worker fail the way a real one does: silently.

        The engine does **not** mark it UNHEALTHY. It stops the heartbeat and,
        for a hard failure, destroys the executor -- and then the control plane
        has to work out on its own that the worker is gone, from the absence of
        beats, and reclaim its jobs through the reaper. Setting the status
        directly would give the failure tests nothing to test.

        ``hard=False`` is a network partition rather than a crash: the process
        survives and its jobs keep running, but the control plane can no longer
        hear it. The reaper reclaims and re-places those jobs anyway, which is
        the correct behaviour and is also how duplicate execution happens in
        real systems -- the abandoned run finishes and its result is discarded
        by the execution-id guard.
        """
        agent = self._workers.get(worker_id)
        if agent is None:
            raise KeyError(worker_id)
        with self._lock:
            self._failed_workers.add(worker_id)
        lost = agent.crash() if hard else ()
        log.error("simulated %s failure of %s", "hard" if hard else "soft", worker_id)
        return {
            "worker_id": worker_id,
            "mode": "crash" if hard else "partition",
            "lost_executions": list(lost),
            "detected_after_s": self.config.heartbeat_timeout_s,
        }

    def recover_worker(self, worker_id: str) -> dict[str, Any]:
        """Bring a failed worker back. UNHEALTHY -> HEALTHY is a demo scenario
        (docs/architecture.md section 5), so recovery has to be real too."""
        agent = self._workers.get(worker_id)
        if agent is None:
            raise KeyError(worker_id)
        with self._lock:
            self._failed_workers.discard(worker_id)
        agent.recover()
        self.registry.heartbeat(worker_id)
        self.registry.set_status(worker_id, WorkerStatus.HEALTHY)
        log.info("recovered %s", worker_id)
        return {"worker_id": worker_id, "status": WorkerStatus.HEALTHY.value}

    def drain_worker(self, worker_id: str) -> dict[str, Any]:
        """Administratively close a worker to new placement.

        Draining is not failure: in-flight jobs run to completion and are never
        reclaimed (see the reaper's ``_unhealthy_workers``). The worker keeps
        heartbeating, so it stays reachable -- it is simply not a candidate.
        """
        if worker_id not in self._workers:
            raise KeyError(worker_id)
        self.registry.set_status(worker_id, WorkerStatus.DRAINING)
        return {"worker_id": worker_id, "status": WorkerStatus.DRAINING.value}

    def undrain_worker(self, worker_id: str) -> dict[str, Any]:
        if worker_id not in self._workers:
            raise KeyError(worker_id)
        self.registry.heartbeat(worker_id)
        self.registry.set_status(worker_id, WorkerStatus.HEALTHY)
        return {"worker_id": worker_id, "status": WorkerStatus.HEALTHY.value}

    def reliability(self) -> dict[str, Any]:
        """Everything Phase 4 added, as numbers.

        Duplicates and abandonments in particular are reported rather than
        logged: a system that claims exactly-once execution and does not
        publish its duplicate rate is claiming something it has not measured.
        """
        with self._lock:
            stats = dict(self._stats)
        agents = {wid: a.stats() for wid, a in self._workers.items()}
        return {
            "retried": stats["retried"],
            "timed_out": stats["timed_out"],
            "worker_lost": stats["worker_lost"],
            "exhausted": stats["exhausted"],
            "dead_lettered": stats["dead_lettered"],
            "stale_results": stats["stale_results"],
            "duplicate_dispatches": sum(a["duplicate_dispatches"] for a in agents.values()),
            "abandoned_executions": sum(a["abandoned_executions"] for a in agents.values()),
            "crashes": sum(a["crashes"] for a in agents.values()),
            "dead_letters": self.queue.dead_letters(),
            "failed_workers": sorted(self._failed_workers),
            "reaper": {
                "sweeps": self.reaper.sweeps,
                "interval_s": self._reaper_interval_s,
                "last": self.reaper.last_report.as_dict() if self.reaper.last_report else None,
            },
            "backoff": {
                "base_s": self.backoff.base_s,
                "cap_s": self.backoff.cap_s,
                "jitter_ratio": self.backoff.jitter_ratio,
            },
            "workers": agents,
        }

    def set_strategy(self, name: str) -> str:
        """Swap the active scheduling strategy. Safe at any time: strategies
        are stateless apart from a rotation cursor, and in-flight jobs are
        unaffected because placement is already decided."""
        scheduler = build_scheduler(
            name,
            aging_interval_s=self.config.aging_interval_s,
            telemetry_enabled=self._telemetry_enabled,
            telemetry_max_age_s=self.config.telemetry_max_age_s,
        )
        with self._lock:
            self.scheduler = scheduler
            now = utcnow()
            self._segments[-1].ended_at = now
            self._segments.append(
                StrategySegment(scheduler.name, scheduler.placement.name, now)
            )
        log.info("strategy switched to %s", name)
        return scheduler.name
