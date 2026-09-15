#!/usr/bin/env python
"""Phase 3 demonstration: the project's core contribution, in one screen.

Set up a fleet where Nimbus's declared/reserved model and the host's real state
disagree:

    worker-1   declared free capacity HIGH   observed CPU ~100%
    worker-2   declared free capacity MED    observed CPU LOW
    worker-3   declared free capacity MED    observed CPU LOW

Submit the same CPU-bound job under both policies. Static Resource-Aware picks
worker-1, because by its information it is the emptiest worker. adaptive-v1
picks worker-2 or worker-3, because it can see that worker-1 is not actually
idle.

This demonstrates the mechanism. It says nothing about global superiority --
for that see scripts/run_phase3_experiments.py and docs/experiments.md.

    python scripts/demo_phase3.py
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
from scheduler import build_scheduler
from scheduler.placement import AdaptivePlacement, StaticResourceAwarePlacement
from scheduler.placement.adaptive import COMPONENTS
from scheduler.replay import replay, verify

BAR = "=" * 82


def rule(title: str) -> None:
    print(f"\n{BAR}\n  {title}\n{BAR}")


def fleet_table(engine: NimbusEngine) -> None:
    print(f"  {'worker':9s} {'DECLARED free':>16s} {'RESERVED':>18s} {'OBSERVED cpu':>14s} "
          f"{'age':>6s} {'fresh':>6s}")
    for w in engine.fleet().workers:
        free = f"{w.available_cpu_units}cpu/{w.free_slots}slot"
        reserved = f"{w.reserved.cpu_units}cpu/{w.reserved.active_jobs}job"
        window = w.observed_window
        cpu = f"{window.cpu_percent_mean:.1f}%" if window else "--"
        age = f"{w.telemetry_age_s:.1f}s" if w.telemetry_age_s is not None else "--"
        fresh = "yes" if (window and window.samples and (w.telemetry_age_s or 99) <= 6.0) else "NO"
        print(f"  {w.worker_id:9s} {free:>16s} {reserved:>18s} {cpu:>14s} {age:>6s} {fresh:>6s}")


def show_decision(label: str, decision) -> None:
    print(f"\n  --- {label}  ({decision.algorithm_version}) ---")
    weights = decision.weights or {}
    print(f"  weights: " + "  ".join(f"{k}={v}" for k, v in weights.items()))
    names = [n for n in COMPONENTS if any(n in c.terms for c in decision.candidates.values())]
    if not names:  # static policy
        names = ["projected_cpu", "projected_memory", "projected_slots"]
    head = "  ".join(f"{n[:9]:>9s}" for n in names)
    print(f"  {'worker':9s} {head}  {'score':>8s}   observed cpu")
    for wid, c in sorted(decision.candidates.items()):
        if c.score is None:
            print(f"  {wid:9s} {'ineligible: ' + (c.ineligible_reason or ''):>{len(head)}s}")
            continue
        row = "  ".join(f"{c.terms.get(n, 0.0):9.3f}" for n in names)
        obs = decision.observed_cpu(wid)
        print(f"  {wid:9s} {row}  {c.score:8.4f}   "
              f"{'--' if obs is None else f'{obs:.1f}%'}")
    print(f"  => SELECTED {decision.selected_worker}")
    print(f"     {decision.reason}")


#: Demos archive separately from experiments. A demo run and an experimental
#: run produce byte-identical artifact directories, so once they share a
#: namespace nothing can tell them apart -- and `scripts/generate_report.py`
#: would read a demo as evidence. See scripts/demo_phase2.py.
DEMO_ARTIFACT_DIR = ".nimbus/demo-artifacts"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--settle", type=float, default=3.0)
    args = parser.parse_args()

    config = NimbusConfig(
        strategy="static_resource_aware", worker_count=3, worker_slots=2,
        telemetry_enabled=True, telemetry_interval_s=0.5, heartbeat_interval_s=2.0,
        telemetry_max_age_s=6.0, artifact_dir=DEMO_ARTIFACT_DIR,
    )
    set_config(config)

    rule("Phase 3 -- does OBSERVED state carry placement information?")
    engine = NimbusEngine(config)
    engine.start()

    stressor = None
    try:
        # 1. Make the host diverge from Nimbus's model.
        cores = engine.registry.cores_for("worker-1")
        stressor = pressure_mod.start_cpu_pressure("worker-1", cores, intensity=1.0)
        print(f"  external pressure: {stressor.describe()}")
        print("  (OS processes -- no Nimbus lease, no slot, no reservation)")

        # 2. Give worker-2 and worker-3 real reservations, so worker-1 is the
        #    *statically* most attractive worker.
        engine.registry.reserve("worker-2", cpu_units=1024, memory_mb=512)
        engine.registry.reserve("worker-3", cpu_units=1024, memory_mb=512)

        time.sleep(args.settle)

        rule("FLEET STATE -- the model and reality disagree")
        fleet_table(engine)
        print("\n  worker-1 has the MOST declared free capacity and the LEAST reserved work,")
        print("  and is simultaneously the only worker that is actually busy.")

        fleet = engine.fleet()
        job = make_job("prime_count", cpu_units=512, memory_mb=256)

        rule("THE SAME JOB, THE SAME FLEET, TWO INFORMATION BUDGETS")
        static = build_scheduler("static_resource_aware").place(job, fleet)
        adaptive = build_scheduler("adaptive", telemetry_max_age_s=6.0).place(job, fleet)
        show_decision("STATIC  (DECLARED + RESERVED)", static)
        show_decision("ADAPTIVE (DECLARED + RESERVED + OBSERVED)", adaptive)

        rule("WHAT CHANGED")
        print(f"  static   -> {static.selected_worker}")
        print(f"  adaptive -> {adaptive.selected_worker}")
        if static.selected_worker == "worker-1" and adaptive.selected_worker != "worker-1":
            print("\n  Static chose the saturated worker because, by its information, it is")
            print("  the emptiest. Adaptive saw the same reservations plus the observed CPU")
            print("  and chose elsewhere. Identical fleet, identical job, identical ordering")
            print("  policy -- the only difference is what placement was allowed to see.")
        else:
            print("\n  NOTE: the expected divergence did not occur in this run.")
            print("  Check that the stressor reached steady state before the decision.")

        rule("REPLAY AND COUNTERFACTUAL")
        for label, decision in (("static", static), ("adaptive", adaptive)):
            result = verify(decision)
            print(f"  {label:9s} replays exactly: {result.matches}  "
                  f"(version {result.algorithm_version})")

        cf_adaptive = replay(static, AdaptivePlacement(telemetry_max_age_s=6.0))
        cf_static = replay(adaptive, StaticResourceAwarePlacement(mode="balance"))
        print(f"\n  counterfactual: adaptive re-scoring the STATIC trace  -> "
              f"{cf_adaptive.selected_worker}")
        print(f"  counterfactual: static re-scoring the ADAPTIVE trace  -> "
              f"{cf_static.selected_worker}")
        print("\n  Both traces carry the full DECLARED + RESERVED + OBSERVED snapshot, so an")
        print("  archived decision can be re-scored under either policy without re-running")
        print("  the workload.")

        rule("ONE FULL ADAPTIVE TRACE")
        payload = json.loads(adaptive.model_dump_json())
        for candidate in payload["candidates"].values():
            if candidate.get("observed"):
                candidate["observed"].pop("cpu_percent_per_core", None)
        print(json.dumps(payload, indent=2)[:2600])

    finally:
        if stressor:
            stressor.stop()
        engine.stop(wait=False)

    rule("PHASE 3 DEMONSTRATION COMPLETE")
    print("  This shows the mechanism only. Whether it helps end to end is measured in")
    print("  scripts/run_phase3_experiments.py and reported in docs/experiments.md.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
