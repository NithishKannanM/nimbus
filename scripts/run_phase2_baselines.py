#!/usr/bin/env python
"""Phase 2 baseline sweep.

Runs every profile through the implemented placement policies and prints the
metric table. Establishes baseline behaviour; proves nothing about adaptive
scheduling, which does not exist yet.

Load balance is reported on utilisation. Job counts are shown alongside because
the two disagree, and only utilisation drives makespan.

    python scripts/run_phase2_baselines.py [--jobs 40] [--trials 1]
"""

from __future__ import annotations

import argparse
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.profiles import PROFILE_NAMES, get_profile
from benchmark.runner import run_experiment
from scheduler import STRATEGIES
from scheduler.replay import verify_all

STRATEGY_ORDER = ("fifo", "static_resource_aware", "static_resource_aware_binpack")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--jobs", type=int, default=40)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--size-scale", type=float, default=0.4)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--profiles", nargs="*", default=list(PROFILE_NAMES))
    args = parser.parse_args()

    assert set(STRATEGY_ORDER) <= set(STRATEGIES)

    print(f"jobs={args.jobs} trials={args.trials} size_scale={args.size_scale} seed={args.seed}")
    print("(single-trial figures are descriptive, not inferential -- "
          "docs/experiments.md section 2)\n")

    rows: list[tuple] = []
    for profile_name in args.profiles:
        profile = get_profile(profile_name)
        print(f"{'=' * 100}\n{profile_name}: {profile.description}\n{'=' * 100}")
        print(f"  {'strategy':30s} {'done':>5s} {'mksp':>6s} {'thr/s':>6s} "
              f"{'p50':>6s} {'p95':>7s} {'utilCV':>7s} {'jobCV':>7s} {'schd_us':>8s}  per-worker util")

        for strategy in STRATEGY_ORDER:
            trial_metrics = []
            replay_bad = 0
            replay_n = 0
            for trial in range(args.trials):
                result = run_experiment(
                    profile=profile, strategy=strategy, job_count=args.jobs,
                    seed=args.seed, size_scale=args.size_scale, trial=trial,
                    telemetry=True, telemetry_interval_s=1.0, warmup_jobs=3,
                    artifact_dir=".nimbus/artifacts", timeout_s=900,
                )
                trial_metrics.append(result.metrics)
                replayable = [
                    d for d in result.decisions
                    if d.selected_worker and d.placement_policy.startswith("static_resource")
                ]
                _, bad = verify_all(replayable)
                replay_n += len(replayable)
                replay_bad += len(bad)

            pick = lambda fn: statistics.median([fn(m) for m in trial_metrics])
            m = trial_metrics[-1]
            util = " ".join(
                f"{w.worker_id[-1]}:{w.utilisation:.2f}" for w in m.workers
            )
            cpu = " ".join(
                f"{w.worker_id[-1]}:{(w.mean_observed_cpu or 0):.0f}%" for w in m.workers
            )
            print(f"  {strategy:30s} {m.jobs_completed:>5d} "
                  f"{pick(lambda x: x.makespan_s):>6.2f} "
                  f"{pick(lambda x: x.throughput_jobs_s):>6.2f} "
                  f"{pick(lambda x: x.completion_ms.p50):>6.0f} "
                  f"{pick(lambda x: x.completion_ms.p95):>7.0f} "
                  f"{pick(lambda x: x.load_balance.utilisation_cv):>7.4f} "
                  f"{pick(lambda x: x.load_balance.job_count_cv):>7.4f} "
                  f"{pick(lambda x: x.scheduling_latency_us.p50):>8.1f}  {util}")
            print(f"  {'':30s} {'':>5s} {'':>6s} {'':>6s} {'':>6s} {'':>7s} {'':>7s} "
                  f"{'':>7s} {'':>8s}  observed cpu {cpu}")
            if replay_n:
                status = "OK" if replay_bad == 0 else f"{replay_bad} MISMATCH"
                print(f"  {'':30s} replay: {replay_n} decisions -> {status}")
            rows.append((profile_name, strategy, m))
        print()

    print(f"{'=' * 100}\nNotes\n{'=' * 100}")
    print("  * utilCV is the load-balance metric; jobCV is descriptive only.")
    print("  * observed cpu is telemetry, recorded during the run and not used for placement.")
    print("  * every archived run is under .nimbus/artifacts/benchmarks/<experiment_id>/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
