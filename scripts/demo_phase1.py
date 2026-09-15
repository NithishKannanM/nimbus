#!/usr/bin/env python
"""Phase 1 demonstration: submit -> queue -> FIFO scheduler -> 3 workers -> result.

Runs the real engine with real process-pool workers executing real computations.
Nothing here is simulated.

    python scripts/demo_phase1.py [--jobs 24]
"""

from __future__ import annotations

import argparse
import random
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import NimbusConfig, set_config
from backend.models.enums import JobStatus
from backend.services.engine import NimbusEngine
from worker.workloads.registry import REGISTRY

# Intensities sized so the demo finishes in seconds while still doing real work.
DEMO_PAYLOADS = {
    "matrix_multiply": {"size": 900},
    "prime_count": {"limit": 8_000_000},
    "hash_chain": {"rounds": 400_000},
    "sort_large": {"elements": 4_000_000},
    "array_transform": {"mb": 96},
    "compress_file": {"mb": 16},
    "file_scan": {"mb": 24},
    "json_transform": {"records": 3_000},
    "metadata_extract": {"count": 150},
}

BAR = "=" * 78


def rule(title: str) -> None:
    print(f"\n{BAR}\n  {title}\n{BAR}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--seed", type=int, default=1337)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    config = NimbusConfig(strategy="fifo", worker_count=3, worker_slots=2, seed=args.seed)
    set_config(config)

    rule("STEP 1 -- start the fleet")
    engine = NimbusEngine(config)
    engine.start()
    for view in engine.fleet().workers:
        cores = engine._cores_for(int(view.worker_id.split("-")[1]) - 1)  # noqa: SLF001
        # DECLARED capacity, not a flat attribute: WorkerState keeps declared,
        # reserved and observed as separate objects, and nothing flattens them.
        print(
            f"  {view.worker_id}  {view.status.value:9s} "
            f"slots={view.declared.slots}  cores={cores}"
        )

    rule(f"STEP 2 -- submit {args.jobs} heterogeneous jobs (strategy: {engine.scheduler.name})")
    job_types = list(REGISTRY)
    submitted = []
    for i in range(args.jobs):
        job_type = job_types[i % len(job_types)]
        job = engine.submit(
            job_type=job_type,
            payload=DEMO_PAYLOADS[job_type],
            priority=rng.choice([1, 2, 3, 3, 4]),
        )
        submitted.append(job)
    by_class = Counter(j.workload_class.value for j in submitted)
    for cls, count in sorted(by_class.items()):
        print(f"  {cls:14s} {count:3d} jobs")

    rule("STEP 3 -- execute")
    started = time.perf_counter()
    while True:
        depth = engine.queue_depth()
        fleet = engine.fleet()
        active = sum(w.reserved.active_jobs for w in fleet.workers)
        counts = engine.store.counts_by_status()
        bars = "  ".join(
            f"{w.worker_id[-1]}:"
            f"{'#' * w.reserved.active_jobs}{'.' * w.free_slots}"
            for w in fleet.workers
        )
        sys.stdout.write(
            f"\r  queued={depth.visible:3d} inflight={depth.in_flight:3d} "
            f"running={active:2d} done={counts['COMPLETED']:3d} "
            f"failed={counts['FAILED']:3d}  slots [{bars}]   "
        )
        sys.stdout.flush()
        if depth.total == 0 and active == 0:
            break
        if time.perf_counter() - started > 300:
            print("\n  TIMED OUT")
            break
        time.sleep(0.2)
    engine.wait_for_idle(timeout_s=30)
    makespan = time.perf_counter() - started
    print()

    rule("STEP 4 -- results")
    jobs = [engine.store.get_job(j.job_id) for j in submitted]
    completed = [j for j in jobs if j.status is JobStatus.COMPLETED]
    failed = [j for j in jobs if j.status is JobStatus.FAILED]

    print(f"  completed          {len(completed)}/{len(jobs)}")
    print(f"  failed             {len(failed)}")
    print(f"  makespan           {makespan:.2f} s")
    if completed:
        print(f"  throughput         {len(completed) / makespan:.2f} jobs/s")
        times = sorted(j.execution_time_ms for j in completed)
        print(f"  exec time  p50     {statistics.median(times):.0f} ms")
        print(f"  exec time  max     {times[-1]:.0f} ms")
        latencies = [
            (j.completed_at - j.created_at).total_seconds() * 1000 for j in completed
        ]
        print(f"  completion p50     {statistics.median(latencies):.0f} ms")

    print("\n  per-worker distribution (FIFO round-robin):")
    placement = Counter(j.assigned_worker for j in completed)
    for worker_id in sorted(placement):
        count = placement[worker_id]
        busy_ms = sum(j.execution_time_ms for j in completed if j.assigned_worker == worker_id)
        print(f"    {worker_id}  {count:3d} jobs  {'#' * count:<26s} busy {busy_ms / 1000:6.2f}s")

    for job in failed:
        print(f"    FAILED {job.job_id} {job.job_type}: {job.error}")

    rule("STEP 5 -- an explainable scheduling decision")
    decision = engine.decisions(limit=1)[0]
    print(f"  {decision.job_id}  ({decision.workload_class.value}, priority {decision.priority})")
    print(f"  strategy           {decision.strategy}")
    print(f"  selected           {decision.selected_worker}")
    # An ineligible candidate has no score -- it was filtered out before
    # scoring, and the trace records why rather than inventing a number for it.
    print("  candidates         " + "   ".join(
        f"{wid}={'-' if cs.score is None else format(cs.score, '.2f')}"
        for wid, cs in sorted(decision.candidates.items())
    ))
    if decision.excluded:
        print(f"  excluded           {decision.excluded}")
    print(f"  runner-up / margin {decision.runner_up} / {decision.margin}")
    print(f"  decision latency   {decision.decision_latency_us:.1f} us")
    print(f"  reason             {decision.reason}")

    latencies = [d.decision_latency_us for d in engine.decisions(limit=1000)]
    print(f"\n  scheduling latency over {len(latencies)} decisions: "
          f"p50={statistics.median(latencies):.1f}us max={max(latencies):.1f}us")

    print(f"\n  dead letters       {engine.queue.dead_letters()}")
    engine.stop()
    stranded = [j for j in jobs if j.status not in (JobStatus.COMPLETED, JobStatus.FAILED)]
    for job in stranded:
        print(f"    STRANDED {job.job_id} {job.job_type}: still {job.status.value}")

    ok = len(completed) == len(jobs) and not engine.queue.dead_letters()
    rule("PHASE 1 PIPELINE VERIFIED" if ok else "PHASE 1 RUN INCOMPLETE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
