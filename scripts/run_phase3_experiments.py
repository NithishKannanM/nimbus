#!/usr/bin/env python
"""Phase 3 experiments: adaptive-v1 vs Static Resource-Aware vs FIFO.

Protocol is fixed by docs/adaptive-v1-preregistration.md section 6:
N repetitions per (profile x scheduler), arms interleaved so machine drift
cannot favour whichever runs last, median + full range + every individual run
value reported. Never the best run.

    python scripts/run_phase3_experiments.py --profiles cpu_pressure --reps 5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.profiles import get_profile
from benchmark.runner import run_experiment
from scheduler.replay import verify_all

PRIMARY = ("fifo", "static_resource_aware", "adaptive")
SECONDARY = ("static_resource_aware_binpack",)


@dataclass
class Cell:
    profile: str
    strategy: str
    runs: list = field(default_factory=list)

    def series(self, fn) -> list[float]:
        return [fn(r.metrics) for r in self.runs]

    def median(self, fn) -> float:
        values = self.series(fn)
        return statistics.median(values) if values else 0.0

    def spread(self, fn) -> tuple[float, float]:
        values = self.series(fn)
        return (min(values), max(values)) if values else (0.0, 0.0)


def run_cells(profile_name, strategies, reps, args) -> dict[str, Cell]:
    profile = get_profile(profile_name)
    cells = {s: Cell(profile_name, s) for s in strategies}
    for rep in range(reps):
        # Interleave: rotate strategy order every repetition.
        order = list(strategies[rep % len(strategies):]) + list(strategies[: rep % len(strategies)])
        for strategy in order:
            result = run_experiment(
                profile=profile, strategy=strategy, job_count=args.jobs,
                seed=args.seed, size_scale=args.size_scale, trial=rep,
                telemetry=True, telemetry_interval_s=args.telemetry_interval,
                warmup_jobs=3, artifact_dir=".nimbus/artifacts", timeout_s=1200,
            )
            cells[strategy].runs.append(result)
            m = result.metrics
            print(f"    [{profile_name}/{strategy}/rep{rep}] {m.headline()} "
                  f"util={m.mean_fleet_utilisation:.3f}", flush=True)
    return cells


def report(profile_name: str, cells: dict[str, Cell]) -> dict:
    profile = get_profile(profile_name)
    print(f"\n{'=' * 104}\n{profile_name}\n  {profile.description}\n{'=' * 104}")

    header = (f"  {'strategy':32s} {'makespan (median [min-max])':>30s} {'thr/s':>7s} "
              f"{'p50':>7s} {'p95':>8s} {'utilCV':>8s} {'fleetU':>7s}")
    print(header)

    summary = {}
    for strategy, cell in cells.items():
        if not cell.runs:
            continue
        mk = cell.median(lambda m: m.makespan_s)
        lo, hi = cell.spread(lambda m: m.makespan_s)
        print(f"  {strategy:32s} {f'{mk:.2f} [{lo:.2f}-{hi:.2f}]':>30s} "
              f"{cell.median(lambda m: m.throughput_jobs_s):>7.2f} "
              f"{cell.median(lambda m: m.completion_ms.p50):>7.0f} "
              f"{cell.median(lambda m: m.completion_ms.p95):>8.0f} "
              f"{cell.median(lambda m: m.load_balance.utilisation_cv):>8.4f} "
              f"{cell.median(lambda m: m.mean_fleet_utilisation):>7.3f}")
        print(f"  {'':32s} runs: "
              + ", ".join(f"{v:.2f}" for v in cell.series(lambda m: m.makespan_s)))

        d = cell.runs[-1].metrics.placement_dynamics
        cpu = " ".join(
            f"{w.worker_id[-1]}:{(w.mean_observed_cpu or 0):.0f}%"
            for w in cell.runs[-1].metrics.workers
        )
        print(f"  {'':32s} switch={d.switch_rate:.2f} conc_max={d.concentration_max:.2f} "
              f"tie_rate={d.tie_rate:.2f} margin={d.mean_margin:.4f} | observed cpu {cpu}")

        replayable = [
            dec for r in cell.runs for dec in r.decisions
            if dec.selected_worker and dec.placement_policy != "round_robin"
        ]
        if replayable:
            _, bad = verify_all(replayable)
            print(f"  {'':32s} replay: {len(replayable)} decisions, "
                  f"{len(bad)} mismatch, version={cell.runs[-1].manifest.algorithm_version}")

        summary[strategy] = {
            "makespan_median": mk,
            "makespan_runs": cell.series(lambda m: m.makespan_s),
            "throughput_median": cell.median(lambda m: m.throughput_jobs_s),
            "p95_median": cell.median(lambda m: m.completion_ms.p95),
            "util_cv_median": cell.median(lambda m: m.load_balance.utilisation_cv),
            "fleet_util_median": cell.median(lambda m: m.mean_fleet_utilisation),
            "tie_rate": cell.runs[-1].metrics.placement_dynamics.tie_rate,
            "switch_rate": cell.runs[-1].metrics.placement_dynamics.switch_rate,
            "concentration_max": cell.runs[-1].metrics.placement_dynamics.concentration_max,
            "algorithm_version": cell.runs[-1].manifest.algorithm_version,
        }

    # Pre-registered materiality test against the best baseline.
    if "adaptive" in summary:
        baselines = {k: v for k, v in summary.items() if k != "adaptive"}
        if baselines:
            best = min(baselines, key=lambda k: baselines[k]["makespan_median"])
            a, b = summary["adaptive"], baselines[best]
            delta = (a["makespan_median"] - b["makespan_median"]) / b["makespan_median"] * 100
            a_lo, a_hi = min(a["makespan_runs"]), max(a["makespan_runs"])
            b_lo, b_hi = min(b["makespan_runs"]), max(b["makespan_runs"])
            disjoint = a_hi < b_lo or b_hi < a_lo
            material = abs(delta) >= 10.0 and disjoint
            verdict = ("MATERIAL " + ("improvement" if delta < 0 else "regression")) if material \
                else "no meaningful observed difference"
            print(f"\n  vs best baseline ({best}): makespan {delta:+.1f}%  "
                  f"ranges {'disjoint' if disjoint else 'OVERLAP'}  ->  {verdict}")
            summary["_comparison"] = {
                "best_baseline": best, "makespan_delta_pct": round(delta, 2),
                "ranges_disjoint": disjoint, "material": material, "verdict": verdict,
            }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profiles", nargs="*",
                        default=["homogeneous_saturated", "cpu_pressure"])
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--jobs", type=int, default=60)
    parser.add_argument("--size-scale", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=1337)
    parser.add_argument("--telemetry-interval", type=float, default=1.0)
    parser.add_argument("--include-binpack", action="store_true")
    parser.add_argument("--out", default="phase3_results.json")
    args = parser.parse_args()

    strategies = PRIMARY + (SECONDARY if args.include_binpack else ())
    print(f"reps={args.reps} jobs={args.jobs} size_scale={args.size_scale} seed={args.seed}")
    print(f"strategies={strategies}")
    print("Load-regime gate (pre-registered): mean fleet utilisation >= 0.50\n")

    results = {}
    for profile_name in args.profiles:
        cells = run_cells(profile_name, strategies, args.reps, args)
        results[profile_name] = report(profile_name, cells)

    Path(args.out).write_text(json.dumps(results, indent=2, default=str))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
