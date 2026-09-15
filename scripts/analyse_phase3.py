#!/usr/bin/env python
"""Post-hoc analysis of archived Phase 3 runs.

Profile B's makespan turned out to be dominated by a single straggler: whichever
large I/O job happens to land on the pressured worker runs 10-40x slower there
and sets the makespan on its own. That makes makespan a very high-variance
statistic and N=5 cannot resolve scheduler differences through it.

So this also reports the quantity the hypothesis is actually about: how much
work each strategy sends to the worker that telemetry says is saturated. That
is a direct measure of whether observed state changed placement, independent of
whether it changed the makespan.

    python scripts/analyse_phase3.py --profile cpu_pressure --pressured worker-1
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def load(root: Path, profile: str):
    runs = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("profile") != profile:
            continue
        metrics = json.loads((manifest_path.parent / "metrics.json").read_text())
        jobs = [
            json.loads(line)
            for line in (manifest_path.parent / "raw.jsonl").read_text().splitlines()
            if line
        ]
        runs.append((manifest, metrics, jobs))
    return runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".nimbus/artifacts/benchmarks")
    parser.add_argument("--profile", default="cpu_pressure")
    parser.add_argument("--pressured", default="worker-1")
    parser.add_argument("--since", default=None,
                        help="only runs started at/after this ISO timestamp")
    args = parser.parse_args()

    runs = load(Path(args.root), args.profile)
    if args.since:
        runs = [r for r in runs if r[0]["started_at"] >= args.since]
    if not runs:
        print("no runs found")
        return 1

    by_strategy = defaultdict(list)
    for manifest, metrics, jobs in runs:
        done = [j for j in jobs if j["status"] == "COMPLETED" and j["execution_time_ms"]]
        if not done:
            continue
        on_pressured = [j for j in done if j["assigned_worker"] == args.pressured]
        total_ms = sum(j["execution_time_ms"] for j in done)
        by_strategy[manifest["strategy"]].append({
            "makespan": metrics["makespan_s"],
            "p95": metrics["completion_ms"]["p95"],
            "util_cv": metrics["load_balance"]["utilisation_cv"],
            "completed": metrics["jobs_completed"],
            "job_share": len(on_pressured) / len(done),
            "work_share": (
                sum(j["execution_time_ms"] for j in on_pressured) / total_ms
                if total_ms else 0.0
            ),
            "longest_s": max(j["execution_time_ms"] for j in done) / 1000.0,
            "version": manifest.get("algorithm_version", "?"),
        })

    fair = 1 / 3
    print(f"profile={args.profile}  pressured={args.pressured}  runs={len(runs)}")
    print(f"fair share for 3 workers = {fair:.1%}\n")
    print(f"  {'strategy':32s} {'n':>3s} {'makespan med [range]':>24s} {'p95':>7s} "
          f"{'utilCV':>7s} {'jobs@w1':>8s} {'work@w1':>8s}")

    summary = {}
    for strategy in sorted(by_strategy):
        rows = by_strategy[strategy]
        mk = [r["makespan"] for r in rows]
        js = [r["job_share"] for r in rows]
        ws = [r["work_share"] for r in rows]
        print(f"  {strategy:32s} {len(rows):>3d} "
              f"{f'{statistics.median(mk):.1f} [{min(mk):.1f}-{max(mk):.1f}]':>24s} "
              f"{statistics.median([r['p95'] for r in rows]):>7.0f} "
              f"{statistics.median([r['util_cv'] for r in rows]):>7.3f} "
              f"{statistics.median(js):>7.1%} {statistics.median(ws):>7.1%}")
        summary[strategy] = {
            "n": len(rows),
            "makespan_median": round(statistics.median(mk), 2),
            "makespan_range": [round(min(mk), 2), round(max(mk), 2)],
            "makespan_runs": [round(v, 2) for v in mk],
            "p95_runs": [round(r["p95"], 1) for r in rows],
            "util_cv_runs": [round(r["util_cv"], 4) for r in rows],
            "job_share_median": round(statistics.median(js), 4),
            "work_share_median": round(statistics.median(ws), 4),
            "job_share_runs": [round(v, 4) for v in js],
            "longest_job_s": [round(r["longest_s"], 1) for r in rows],
            "truncated_runs": sum(1 for r in rows if r["completed"] < 60),
        }

    print("\n  per-run share of jobs sent to the pressured worker:")
    for strategy in sorted(summary):
        series = " ".join(f"{v:.0%}" for v in summary[strategy]["job_share_runs"])
        print(f"    {strategy:32s} {series}")

    print("\n  longest single job per run (seconds) -- this sets the makespan:")
    for strategy in sorted(summary):
        series = " ".join(f"{v:.0f}" for v in summary[strategy]["longest_job_s"])
        print(f"    {strategy:32s} {series}")

    truncated = sum(s["truncated_runs"] for s in summary.values())
    if truncated:
        print(f"\n  WARNING: {truncated} run(s) completed fewer than 60 jobs "
              f"(pre-fix wait_for_idle truncation)")

    if "adaptive" in summary:
        a = summary["adaptive"]["job_share_median"]
        for baseline in ("fifo", "static_resource_aware", "static_resource_aware_binpack"):
            if baseline in summary:
                b = summary[baseline]["job_share_median"]
                print(f"\n  adaptive sends {a:.1%} of jobs to the pressured worker vs "
                      f"{b:.1%} for {baseline} "
                      f"({(a - b) / b * 100:+.0f}% relative)")

    Path("phase3_analysis.json").write_text(json.dumps(summary, indent=2))
    print("\nwrote phase3_analysis.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
