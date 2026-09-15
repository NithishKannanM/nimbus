#!/usr/bin/env python
"""Phase 2 demonstration.

  A. Static Resource-Aware placement, driven by DECLARED capacity and Nimbus
     RESERVATIONS.
  B. Telemetry isolation: external CPU pressure on the statically preferred
     worker is observed and recorded, and deliberately not acted on.
  C. A complete decision trace.
  D. The telemetry ON/OFF control, with measured overhead.

Section B is the one to read carefully. The scheduler picking a worker that is
pegged at ~100% CPU looks like a bug and is not: it is the control that lets
Phase 3 measure whether observed telemetry carries placement information that
declared capacity does not.

    python scripts/demo_phase2.py [--jobs 40] [--skip-control]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import NimbusConfig, set_config
from backend.models.workload_helpers import make_job
from backend.services.engine import NimbusEngine
from benchmark import pressure as pressure_mod
from benchmark.runner import compare_telemetry_control
from scheduler import build_scheduler
from scheduler.replay import verify_all

BAR = "=" * 78


def rule(title: str) -> None:
    print(f"\n{BAR}\n  {title}\n{BAR}")


def fleet_table(engine: NimbusEngine) -> None:
    print(f"  {'worker':9s} {'DECLARED':>18s}  {'RESERVED':>18s}  {'OBSERVED':>26s}")
    for w in engine.fleet().workers:
        declared = f"{w.declared.cpu_units}cpu/{w.declared.memory_mb}MB/{w.declared.slots}s"
        reserved = f"{w.reserved.cpu_units}cpu/{w.reserved.memory_mb}MB/{w.reserved.active_jobs}j"
        observed = "-- (telemetry off)" if not w.observed else (
            f"cpu={w.observed.cpu_percent:5.1f}% mem={w.observed.memory_percent:5.1f}%"
        )
        print(f"  {w.worker_id:9s} {declared:>18s}  {reserved:>18s}  {observed:>26s}")


def wait_for_telemetry(engine: NimbusEngine, ticks: int = 3, timeout_s: float = 12.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if engine.telemetry.stats.ticks >= ticks:
            return True
        time.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
def demo_a(engine: NimbusEngine) -> None:
    rule("DEMO A -- Static Resource-Aware placement (declared + reserved only)")

    # Reserve directly through the registry: this is control-plane bookkeeping,
    # exactly what a real placement would have produced.
    engine.registry.reserve("worker-1", cpu_units=1536, memory_mb=3072)  # heavily reserved
    engine.registry.reserve("worker-3", cpu_units=512, memory_mb=1024)   # medium
    # worker-2 left untouched -- lightly reserved
    fleet_table(engine)

    job = make_job("matrix_multiply", cpu_units=512, memory_mb=512, priority=2)
    decision = engine.scheduler.place(job, engine.fleet())

    print(f"\n  incoming job: {job.workload_class.value} "
          f"cpu_units={job.cpu_units} memory_mb={job.memory_mb} priority={job.priority}")
    print(f"  weights: {decision.weights}")
    print(f"\n  {'worker':9s} {'proj cpu':>9s} {'proj mem':>9s} {'proj slot':>10s} "
          f"{'score':>8s}  eligible")
    for wid, c in sorted(decision.candidates.items()):
        if c.score is None:
            print(f"  {wid:9s} {'--':>9s} {'--':>9s} {'--':>10s} {'--':>8s}  "
                  f"NO: {c.ineligible_reason}")
            continue
        t = c.terms
        print(f"  {wid:9s} {t['projected_cpu']:9.4f} {t['projected_memory']:9.4f} "
              f"{t['projected_slots']:10.4f} {c.score:8.4f}  yes")
    print(f"\n  SELECTED: {decision.selected_worker}  "
          f"(runner-up {decision.runner_up}, margin {decision.margin:.4f})")
    print(f"  reason: {decision.reason}")

    for worker_id, cpu, mem in (("worker-1", 1536, 3072), ("worker-3", 512, 1024)):
        engine.registry.release(worker_id, cpu_units=cpu, memory_mb=mem)


# --------------------------------------------------------------------------- #
def demo_b(engine: NimbusEngine, job_count: int) -> None:
    rule("DEMO B -- Telemetry isolation under external CPU pressure")

    cores = engine.registry.cores_for("worker-1")
    stressor = pressure_mod.start_cpu_pressure("worker-1", cores, intensity=1.0)
    print(f"  external pressure: {stressor.describe()}")
    print("  (plain OS processes -- they never import Nimbus, hold no lease,")
    print("   and consume no slot, so Nimbus's reserved model cannot see them)")

    wait_for_telemetry(engine, ticks=3)
    time.sleep(1.0)
    print()
    fleet_table(engine)

    w1 = engine.fleet().by_id("worker-1")
    print(f"\n  worker-1 is observed at {w1.observed.cpu_percent:.1f}% CPU, yet Nimbus's")
    print(f"  static model still reports {w1.available_cpu_units}/{w1.declared.cpu_units} "
          f"cpu units and {w1.free_slots}/{w1.declared.slots} slots free.")

    try:
        jobs = [
            engine.submit(job_type="prime_count", payload={"limit": 600_000})
            for _ in range(job_count)
        ]
        engine.wait_for_idle(timeout_s=300)
    finally:
        stressor.stop()

    placements: dict[str, int] = {}
    for job in jobs:
        worker = engine.store.get_job(job.job_id).assigned_worker
        placements[worker] = placements.get(worker, 0) + 1

    print(f"\n  placement of {len(jobs)} CPU-bound jobs while worker-1 was saturated:")
    for worker_id in sorted(placements):
        count = placements[worker_id]
        marker = "  <- externally saturated" if worker_id == "worker-1" else ""
        print(f"    {worker_id}  {count:3d} jobs  {'#' * count}{marker}")

    share = placements.get("worker-1", 0) / len(jobs)
    print(f"\n  worker-1 received {share:.0%} of the work. EXPECTED: static placement")
    print("  cannot see observed CPU, so it does not avoid a pressured worker.")
    print("  This is the Phase 2 baseline, not a defect.")


# --------------------------------------------------------------------------- #
def demo_c(engine: NimbusEngine) -> None:
    rule("DEMO C -- Complete decision trace")

    decision = next(
        (d for d in engine.decisions(limit=200) if d.selected_worker), None
    )
    if decision is None:
        print("  no placement decisions recorded")
        return

    payload = json.loads(decision.model_dump_json())
    # Trim per-core arrays so the trace fits on a screen; everything else is verbatim.
    for candidate in payload["candidates"].values():
        if candidate.get("observed"):
            candidate["observed"].pop("cpu_percent_per_core", None)
    print(json.dumps(payload, indent=2)[:3400])

    print("\n  Note the shape of this record:")
    print("    * ordering_policy and placement_policy are separate fields")
    print("    * every candidate carries DECLARED, RESERVED and OBSERVED blocks")
    print("    * 'terms' contains only projected_* values -- all from DECLARED/RESERVED")
    print("    * 'observed' is present and was NOT an input to 'score'")

    placed = [d for d in engine.decisions(limit=500) if d.selected_worker]
    replayable = [d for d in placed if d.placement_policy.startswith("static_resource_aware")]
    if replayable:
        matched, mismatched = verify_all(replayable)
        print(f"\n  Replay check: {len(matched)}/{len(replayable)} decisions reproduced "
              f"exactly from their recorded DECLARED + RESERVED state alone.")
        if mismatched:
            print(f"  MISMATCHES: {[m.decision_id for m in mismatched[:5]]}")
        else:
            print("  Nothing outside the static state influenced any placement.")


# --------------------------------------------------------------------------- #
#: Demos archive here, NOT into .nimbus/artifacts.
#:
#: A demo run and an experimental run produce byte-identical artifact
#: directories -- same manifest, same metrics, same shape -- so once they share
#: a namespace nothing can tell them apart again. A 12-job demo would then be
#: read by `scripts/generate_report.py` as evidence and appear in the exclusion
#: list of `docs/results.md`, which is at best noise and at worst a small
#: run quietly widening a comparison's range.
#:
#: Separating them costs one constant. Recovering from a contaminated archive
#: costs reading timestamps and guessing.
DEMO_ARTIFACT_DIR = ".nimbus/demo-artifacts"


def demo_d(job_count: int, trials: int) -> None:
    rule("DEMO D -- Telemetry ON/OFF control and measured overhead")
    print(f"  Same profile, same seed, same strategy; telemetry toggled.")
    print(f"  {trials} trial(s) per arm, arms alternated to cancel machine drift.\n")

    control = compare_telemetry_control(
        profile="homogeneous_saturated", job_count=job_count, seed=1337,
        size_scale=0.4, telemetry_interval_s=1.0, warmup_jobs=3, trials=trials,
        artifact_dir=DEMO_ARTIFACT_DIR,
    )

    print(f"  {'arm':5s} {'thr/s (per trial)':>34s} {'median':>8s} {'p50 ms':>8s} {'util CV':>9s}")
    for arm in ("off", "on"):
        results = control[arm]
        series = ", ".join(f"{r.metrics.throughput_jobs_s:.2f}" for r in results)
        median = control[f"throughput_{arm}"]
        median_value = median[len(median) // 2] if median else 0.0
        p50 = results[-1].metrics.completion_ms.p50
        cv = results[-1].metrics.load_balance.utilisation_cv
        print(f"  {arm:5s} {series:>34s} {median_value:>8.2f} {p50:>8.0f} {cv:>9.4f}")

    print(f"\n  placement determinism (the strong claim):")
    print(f"    {control['replay_checked']} decisions replayed from recorded "
          f"DECLARED + RESERVED state")
    print(f"    mismatches: {control['replay_mismatches']}  ->  "
          f"deterministic={control['placement_deterministic']}")

    overhead = control["overhead"]
    print(f"\n  telemetry overhead (measured, not assumed):")
    print(f"    ticks={overhead.get('ticks', 0):.0f} samples={overhead.get('samples', 0):.0f} "
          f"errors={overhead.get('errors', 0):.0f}")
    print(f"    mean sample {overhead.get('mean_sample_ms', 0):.3f} ms, "
          f"mean tick {overhead.get('mean_tick_ms', 0):.3f} ms")
    print(f"    duty cycle  {overhead.get('duty_cycle', 0) * 100:.4f}% of wall time")

    off, on = control["throughput_off"], control["throughput_on"]
    print(f"\n    median throughput delta ON vs OFF: {control['throughput_delta_pct']:+.1f}%")
    if off and on:
        spread_off = max(off) - min(off)
        spread_on = max(on) - min(on)
        print(f"    within-arm spread: off {spread_off:.2f}/s, on {spread_on:.2f}/s")
        if trials < 3:
            print("    NOTE: too few trials to separate overhead from noise. The duty-cycle")
            print("    figure above is the defensible overhead measurement; this delta is not.")
        elif abs(control["throughput_delta_pct"]) < 100 * max(spread_off, spread_on) / max(
            sum(off) / len(off), 1e-9
        ):
            print("    The delta is smaller than within-arm variation, so this run does not")
            print("    show a throughput cost. It also does not prove there is none.")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=24)
    parser.add_argument("--skip-control", action="store_true")
    parser.add_argument("--trials", type=int, default=3,
                        help="trials per arm for the telemetry ON/OFF control")
    args = parser.parse_args()

    config = NimbusConfig(
        strategy="static_resource_aware",
        worker_count=3, worker_slots=2,
        telemetry_enabled=True, telemetry_interval_s=0.5, heartbeat_interval_s=2.0,
        artifact_dir=DEMO_ARTIFACT_DIR,
    )
    set_config(config)

    rule("Phase 2 -- Static Resource-Aware placement + telemetry instrumentation")
    engine = NimbusEngine(config)
    engine.start()
    print(f"  strategy         {engine.scheduler.name}")
    print(f"  ordering policy  {engine.scheduler.ordering.name}")
    print(f"  placement policy {engine.scheduler.placement.name}")
    print(f"  available        {', '.join(sorted(build_scheduler.__globals__['STRATEGIES']))}")
    print(f"  telemetry        enabled={engine.telemetry_enabled} "
          f"interval={config.telemetry_interval_s}s")

    try:
        wait_for_telemetry(engine, ticks=2)
        demo_a(engine)
        demo_b(engine, args.jobs)
        demo_c(engine)
    finally:
        engine.stop(wait=False)

    if not args.skip_control:
        demo_d(args.jobs, args.trials)

    rule("PHASE 2 DEMONSTRATION COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
