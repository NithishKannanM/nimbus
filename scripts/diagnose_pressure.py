#!/usr/bin/env python
"""Diagnose whether external pressure actually lands on the intended worker.

Phase 3's Profile B showed a 4x makespan swing between repetitions of the same
scheduler, which under section 34 of the brief must be treated as a suspected
experimental bug rather than a finding. This checks the pressure rig itself:
core assignment, stressor liveness, and the CPU differential telemetry actually
observes before the measured workload starts.

    python scripts/diagnose_pressure.py --reps 6
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.config import NimbusConfig, set_config
from backend.services.engine import NimbusEngine
from benchmark import pressure as pressure_mod


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reps", type=int, default=6)
    parser.add_argument("--settle", type=float, default=1.0,
                        help="matches the runner's pre-workload settle time")
    args = parser.parse_args()

    print(f"{'rep':>3s} {'cores(w1)':>22s} {'alive':>5s} "
          f"{'w1 cpu':>8s} {'w2 cpu':>8s} {'w3 cpu':>8s} {'differential':>13s}")

    failures = 0
    for rep in range(args.reps):
        config = NimbusConfig(
            strategy="static_resource_aware", worker_count=3, worker_slots=2,
            telemetry_enabled=True, telemetry_interval_s=0.5,
            heartbeat_interval_s=2.0, artifact_dir=".nimbus/diag",
        )
        set_config(config)
        engine = NimbusEngine(config)
        engine.start()
        stressor = None
        try:
            cores = engine.registry.cores_for("worker-1")
            stressor = pressure_mod.start_cpu_pressure("worker-1", cores, intensity=1.0)
            time.sleep(args.settle)

            fleet = engine.fleet()
            readings = {
                w.worker_id: (w.observed_window.cpu_percent_mean if w.observed_window else -1.0)
                for w in fleet.workers
            }
            w1 = readings.get("worker-1", -1.0)
            others = [v for k, v in readings.items() if k != "worker-1"]
            differential = w1 - (max(others) if others else 0.0)
            ok = differential > 40.0
            failures += 0 if ok else 1

            print(f"{rep:>3d} {str(list(cores)):>22s} {stressor.alive:>5d} "
                  f"{w1:>7.1f}% {others[0]:>7.1f}% {others[1]:>7.1f}% "
                  f"{differential:>+12.1f}{'' if ok else '  <-- NOT ISOLATED'}")
        finally:
            if stressor:
                stressor.stop()
            engine.stop(wait=False)

    print(f"\nruns where pressure failed to isolate on worker-1: {failures}/{args.reps}")
    print(f"settle time used: {args.settle}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
