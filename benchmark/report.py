"""Report generation from archived runs.

Every figure in a report is **derived from the archive**, never passed in. That
is the whole constraint: ``docs/experiments.md`` requires that any number in the
final write-up be regenerable from ``benchmarks/{run_id}/`` alone, so this
module reads those files and nothing else. If a number cannot be produced this
way, it does not go in the report.

Three things are always reported together, and the Phase 3 experience is why:

1. **The pre-registered criterion's verdict.** Whatever it says.
2. **The standing statistical protocol's verdict** (docs/experiments.md
   section 2.3, fixed in Phase 1) -- Mann-Whitney, effect size, Bonferroni.
3. **The load regime**, because a comparison run below 50% fleet utilisation is
   inadmissible as primary evidence regardless of how good its numbers look
   (the R9 confound, docs/adaptive-v1-preregistration.md section 6.3).

In Phase 3 the first said "no meaningful difference" and the second said
p=0.0039 with a large effect. Publishing only one of those would have been a
choice made after seeing the answer, so the report publishes both and says
which is which.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from benchmark.analysis import Series, compare_all

#: Metrics pulled from each run's metrics.json, as (label, dotted path).
REPORTED_METRICS: dict[str, tuple[str, ...]] = {
    "makespan_s": ("makespan_s",),
    "throughput_jobs_s": ("throughput_jobs_s",),
    "completion_p50_ms": ("completion_ms", "p50"),
    "completion_p95_ms": ("completion_ms", "p95"),
    "queue_wait_p95_ms": ("queue_wait_ms", "p95"),
    "utilisation_cv": ("load_balance", "utilisation_cv"),
    "jain_fairness": ("load_balance", "jain_fairness"),
    "mean_fleet_utilisation": ("mean_fleet_utilisation",),
    "scheduling_latency_p95_us": ("scheduling_latency_us", "p95"),
}


@dataclass
class ArchivedRun:
    run_id: str
    manifest: dict[str, Any]
    metrics: dict[str, Any]
    path: Path

    @property
    def strategy(self) -> str:
        return self.manifest.get("strategy", "?")

    @property
    def profile(self) -> str:
        return self.manifest.get("profile", "?")

    @property
    def algorithm_version(self) -> str:
        return self.manifest.get("algorithm_version", "unversioned")

    @property
    def completed(self) -> int:
        return int(self.metrics.get("jobs_completed", 0))

    @property
    def submitted(self) -> int:
        return int(self.metrics.get("jobs_submitted", 0))

    @property
    def truncated(self) -> bool:
        """Fewer jobs finished than were planned.

        A truncated run still produces a makespan and a throughput, which is
        precisely why it has to be detected rather than trusted: three Phase 3
        runs silently completed 59 of 60 jobs and reported perfectly ordinary
        numbers.
        """
        planned = int(self.manifest.get("job_count", 0))
        return bool(planned and self.completed < planned)

    def value(self, metric: str) -> float | None:
        path = REPORTED_METRICS.get(metric)
        if path is None:
            return None
        node: Any = self.metrics
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return None
            node = node[key]
        return float(node) if isinstance(node, (int, float)) else None


def load_runs(root: str | Path, *, profile: str | None = None) -> list[ArchivedRun]:
    """Read every archived run under ``root``, newest last.

    Silently skips directories missing a manifest or metrics: a run that was
    interrupted mid-archive is not a run, and treating a partial archive as
    data would put half a run's numbers into a comparison.
    """
    base = Path(root)
    runs: list[ArchivedRun] = []
    for manifest_path in sorted(base.glob("*/manifest.json")):
        metrics_path = manifest_path.parent / "metrics.json"
        if not metrics_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
            metrics = json.loads(metrics_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if profile is not None and manifest.get("profile") != profile:
            continue
        runs.append(
            ArchivedRun(
                run_id=manifest.get("experiment_id", manifest_path.parent.name),
                manifest=manifest,
                metrics=metrics,
                path=manifest_path.parent,
            )
        )
    runs.sort(key=lambda r: r.manifest.get("started_at", ""))
    return runs


@dataclass
class ProfileReport:
    profile: str
    runs: list[ArchivedRun] = field(default_factory=list)
    treatment: str = "adaptive"

    def series(self, metric: str) -> dict[str, Series]:
        by_strategy: dict[str, Series] = {}
        for run in self.runs:
            value = run.value(metric)
            if value is None:
                continue
            series = by_strategy.setdefault(
                run.strategy, Series(strategy=run.strategy, metric=metric)
            )
            series.values.append(value)
        return by_strategy

    def admissible(self) -> tuple[list[ArchivedRun], list[str]]:
        """Runs usable as primary evidence, and why the rest are not.

        Two exclusions, both pre-registered rather than invented here:

        * **Truncated runs.** A run that lost jobs is measuring a bug.
        * **Low load.** Below 50% mean fleet utilisation the static baseline
          degenerates into its worker_id tie-break (the R9 confound), so
          adaptive would beat it for a reason that has nothing to do with
          telemetry.
        """
        keep: list[ArchivedRun] = []
        reasons: list[str] = []
        for run in self.runs:
            if run.truncated:
                reasons.append(
                    f"{run.run_id} ({run.strategy}): truncated, "
                    f"{run.completed}/{run.manifest.get('job_count')} completed"
                )
                continue
            utilisation = run.value("mean_fleet_utilisation") or 0.0
            if utilisation < 0.50:
                reasons.append(
                    # Four decimals, not two. At two, a run excluded at 0.4996
                    # reads as "0.50 < 0.50" -- a message that appears to
                    # contradict itself and makes the gate look broken when it
                    # is working correctly.
                    f"{run.run_id} ({run.strategy}): mean fleet utilisation "
                    f"{utilisation:.4f} < 0.50, R9 low-load confound"
                )
                continue
            keep.append(run)
        return keep, reasons

    def comparisons(self, metric: str, *, admissible_only: bool = True) -> list[dict[str, Any]]:
        source = self if not admissible_only else ProfileReport(
            profile=self.profile, runs=self.admissible()[0], treatment=self.treatment
        )
        return [c.as_dict() for c in compare_all(
            source.series(metric), treatment=self.treatment, metric=metric
        )]

    def tie_break_rates(self) -> dict[str, float]:
        """Share of placements decided by the ``worker_id`` tie-break.

        The Phase 3 confound, and the reason ``adaptive`` vs ``fifo`` is the
        clean comparison: the static policies decided 35-36% of placements this
        way, so part of adaptive's edge over them is "not tying" rather than
        telemetry content. ``fifo`` never ties.

        Read from decisions.jsonl, which means it is only available for runs
        whose decision trace was archived -- as it should be, since this is a
        claim about individual decisions.
        """
        totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for run in self.runs:
            decisions_path = run.path / "decisions.jsonl"
            if not decisions_path.exists():
                continue
            for line in decisions_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    decision = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not decision.get("selected_worker"):
                    continue
                counts = totals[run.strategy]
                counts[1] += 1
                if _was_a_tie(decision):
                    counts[0] += 1
        return {
            strategy: round(tied / total, 4) if total else 0.0
            for strategy, (tied, total) in sorted(totals.items())
        }


def _was_a_tie(decision: dict[str, Any]) -> bool:
    """Whether the runner-up scored identically to the winner.

    Exact float equality, deliberately. A tie-break on ``worker_id`` fires only
    on exact equality, so anything looser would count near-ties as ties and
    overstate the confound this metric exists to expose.
    """
    margin = decision.get("margin")
    if margin is not None:
        return float(margin) == 0.0
    scores = [
        candidate.get("score")
        for candidate in (decision.get("candidates") or {}).values()
        if candidate.get("eligible") and candidate.get("score") is not None
    ]
    if len(scores) < 2:
        return False
    best = min(scores)
    return sum(1 for s in scores if s == best) > 1


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def render_markdown(report: ProfileReport, *, metric: str = "makespan_s") -> str:
    """A profile's results as Markdown.

    Deliberately plain text rather than a chart. The claims here are about
    medians, ranges and p-values on N≈9, and a chart of nine points invites the
    reader to see a trend that the statistics do not support.
    """
    admissible, excluded = report.admissible()
    lines: list[str] = [
        f"# Benchmark report — profile `{report.profile}`",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
        f"from {len(report.runs)} archived run(s).",
        "",
        "Every figure below is derived from `benchmarks/{run_id}/` and nothing else.",
        "",
    ]

    by_strategy = _versions_by_strategy(report.runs)
    lines += [
        "## Provenance",
        "",
        f"- Runs archived: **{len(report.runs)}**, "
        f"admissible as primary evidence: **{len(admissible)}**",
        "",
        "| strategy | runs | algorithm version |",
        "|---|---:|---|",
    ]
    for strategy in sorted(by_strategy):
        versions, count = by_strategy[strategy]
        lines.append(
            f"| `{strategy}` | {count} | {', '.join(f'`{v}`' for v in sorted(versions))} |"
        )
    lines.append("")

    # Different strategies having different version strings is expected -- they
    # are different policies. What is not expected is *one* strategy appearing
    # under two versions, which means a formula changed mid-experiment and the
    # earlier runs measure a different scheduler.
    mixed = {s: v for s, (v, _) in by_strategy.items() if len(v) > 1}
    if mixed:
        lines += [
            "> **A strategy appears under more than one algorithm version:** "
            + ", ".join(f"`{s}` ({', '.join(sorted(v))})" for s, v in sorted(mixed.items()))
            + ". Those runs are not comparable to each other -- a formula change "
            "makes the earlier ones measurements of a different scheduler "
            "(risk R11). Split this report by version before drawing any "
            "conclusion.",
            "",
        ]

    if excluded:
        truncated = [r for r in excluded if "truncated" in r]
        low_load = [r for r in excluded if "utilisation" in r]
        lines += [
            "### Excluded from primary evidence",
            "",
            f"{len(truncated)} truncated, {len(low_load)} below the load-regime gate.",
            "",
            "<details><summary>per-run exclusions</summary>",
            "",
        ]
        lines += [f"- {reason}" for reason in excluded]
        lines += ["", "</details>", ""]
        lines += [
            "These exclusions are pre-registered, not chosen after seeing the "
            "numbers: a truncated run is measuring a bug, and §6.3 requires "
            "mean fleet utilisation ≥ 0.50 for a run to be primary evidence.",
            "",
        ]

    # The gate's own documented failure mode. Stating it here rather than
    # letting a reader conclude the runs were simply too light.
    if low_load_dominates(report):
        lines += [
            "> **The load-regime gate mis-fires on this profile, and this is "
            "already on the record.** "
            "`docs/adaptive-v1-preregistration.md` §7 states it: the gate "
            "assumed the R9 tie-break degeneracy was a low-load phenomenon, and "
            "on an external-pressure profile mean utilisation *falls as a "
            "consequence of the effect being studied* — a scheduler that "
            "successfully steers work away from a saturated worker leaves that "
            "worker idle from Nimbus's point of view, which lowers measured "
            "fleet utilisation. The gate then excludes exactly the runs where "
            "the effect is largest.",
            ">",
            "> So the comparisons below are computed over **all** runs and are "
            "reported as boundary-condition/descriptive results per §6.3, not "
            "as primary evidence for H3. The tie-break table further down is "
            "the direct measure of the R9 confound that the gate was a proxy "
            "for, and it is the number to read instead.",
            "",
        ]
        scope = report.runs
        scope_label = "all archived runs (gate mis-fires — see above)"
    else:
        scope = admissible
        scope_label = "admissible runs only"

    lines += [f"_Results below computed over: **{scope_label}**._", ""]
    lines += _metric_table(ProfileReport(report.profile, scope, report.treatment))
    lines += _comparison_section(report, metric, scope)
    lines += _tie_break_section(report)
    lines += [
        "## What this report does not claim",
        "",
        "- Nothing about regimes that were not run. Every statement is bounded "
        "by the profiles and load levels present above.",
        "- Nothing about AWS. These are local runs; three processes pinned to "
        "disjoint cores on one host share a page cache and memory bandwidth in "
        "a way three EC2 instances do not (`docs/limitations.md` §3).",
        "- Nothing about Nimbus versus AWS Batch or ECS unless a Batch run "
        "appears above under its own strategy label.",
        "",
    ]
    return "\n".join(lines)


def _versions_by_strategy(
    runs: list[ArchivedRun],
) -> dict[str, tuple[set[str], int]]:
    out: dict[str, tuple[set[str], int]] = {}
    for run in runs:
        versions, count = out.get(run.strategy, (set(), 0))
        versions.add(run.algorithm_version)
        out[run.strategy] = (versions, count + 1)
    return out


def profile_has_pressure(profile: str) -> bool:
    """Whether this profile applies external host pressure.

    Load-bearing for interpreting the load gate. Unknown profile names are
    treated as unpressured, which is the conservative direction: it keeps the
    gate's verdict rather than explaining it away.
    """
    try:
        from benchmark.profiles import PROFILES  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return False
    spec = PROFILES.get(profile)
    return bool(spec and spec.pressure)


def low_load_dominates(report: ProfileReport, *, threshold: float = 0.5) -> bool:
    """Whether the load gate is filtering the experiment rather than filtering noise.

    Two conditions, both required:

    * The gate excludes most of the profile's runs, **and**
    * the profile applies external pressure.

    The second is what stops this from being a licence to ignore the gate
    whenever it is inconvenient. On a pressure profile the mis-fire is real and
    documented (docs/adaptive-v1-preregistration.md section 7): a scheduler that
    successfully steers work away from a saturated worker leaves that worker
    idle *from Nimbus's point of view*, so mean fleet utilisation falls as a
    consequence of the effect being studied, and the gate excludes exactly the
    runs where the effect is largest.

    On a profile with no external pressure there is no such mechanism. Low
    utilisation there means the runs really were too light, the R9 tie-break
    confound really is in play, and the gate's verdict stands.
    """
    if not report.runs or not profile_has_pressure(report.profile):
        return False
    admissible, _ = report.admissible()
    return len(admissible) / len(report.runs) < threshold


def _metric_table(report: ProfileReport) -> list[str]:
    lines = ["## Results", ""]
    for metric in REPORTED_METRICS:
        series = report.series(metric)
        if not series:
            continue
        lines += [
            f"### `{metric}`",
            "",
            "| strategy | n | median | IQR | range | 95% CI (median) |",
            "|---|---:|---:|---|---|---|",
        ]
        for name in sorted(series):
            entry = series[name].as_dict()
            lines.append(
                f"| `{name}` | {entry['n']} | {entry['median']:g} | "
                f"{entry['iqr'][0]:g}–{entry['iqr'][1]:g} | "
                f"{entry['range'][0]:g}–{entry['range'][1]:g} | "
                f"{entry['ci95'][0]:g}–{entry['ci95'][1]:g} |"
            )
        lines.append("")
    return lines


def _comparison_section(
    report: ProfileReport, metric: str, admissible: list[ArchivedRun]
) -> list[str]:
    scoped = ProfileReport(report.profile, admissible, report.treatment)
    comparisons = [c.as_dict() for c in compare_all(
        scoped.series(metric), treatment=report.treatment, metric=metric
    )]
    if not comparisons:
        return [
            "## Comparisons",
            "",
            f"No comparison possible: `{report.treatment}` is absent from the "
            "admissible runs, or there is no baseline to compare it against.",
            "",
        ]

    alpha = comparisons[0]["alpha"]
    lines = [
        "## Comparisons",
        "",
        f"Mann-Whitney U, two-sided, Bonferroni-corrected across "
        f"{len(comparisons)} comparison(s): α = {alpha:.4f}. "
        f"Protocol fixed in `docs/experiments.md` §2.3, before any of these "
        f"runs existed.",
        "",
        "| comparison | median Δ | p | Cliff's δ | effect | significant | ranges disjoint |",
        "|---|---:|---:|---:|---|---|---|",
    ]
    for c in comparisons:
        lines.append(
            f"| {c['treatment']} vs `{c['baseline']}` | {c['median_delta_pct']:+.1f}% | "
            f"{c['p_value']:.4f} | {c['cliffs_delta']:+.2f} | {c['effect_label']} | "
            f"{'yes' if c['significant'] else 'no'} | "
            f"{'yes' if c['ranges_disjoint'] else 'no'} |"
        )
    lines.append("")

    # The two verdicts, reported together, whichever way they fall.
    disagreements = [
        c for c in comparisons if c["significant"] != c["ranges_disjoint"]
    ]
    if disagreements:
        lines += [
            "> **The two criteria disagree, and both are reported.**",
            ">",
            "> The pre-registration required a ≥10% median difference *and* "
            "non-overlapping per-run ranges. The standing statistical protocol "
            "asks for a significant non-parametric test with an effect size. "
            "On a bimodal distribution at N≈9, requiring disjoint ranges is "
            "nearly unsatisfiable regardless of effect size — one unlucky "
            "treatment run and one lucky baseline run overlap the ranges while "
            "leaving the medians far apart.",
            ">",
            "> That is a flaw in the pre-registered criterion, not evidence "
            "against the statistics. But the criterion was fixed in advance and "
            "the protocol was fixed in Phase 1, so neither may be dropped after "
            "seeing which one is more favourable. Both verdicts stand above.",
            "",
        ]
    return lines


def _tie_break_section(report: ProfileReport) -> list[str]:
    rates = report.tie_break_rates()
    if not rates:
        return []
    lines = [
        "## Confound: placements decided by the `worker_id` tie-break",
        "",
        "| strategy | share of placements decided by tie-break |",
        "|---|---:|",
    ]
    for strategy, rate in rates.items():
        lines.append(f"| `{strategy}` | {rate:.1%} |")
    lines += [
        "",
        "A placement decided by the tie-break was not decided by the policy's "
        "scoring. Where a static policy ties often and `adaptive` does not — "
        "a continuous observed signal rarely produces exact equality — part of "
        "any measured advantage is *not tying* rather than telemetry content.",
        "",
        "`fifo` (round-robin) never ties, which makes **adaptive vs `fifo`** the "
        "comparison free of this confound.",
        "",
    ]
    return lines


def build_report(
    artifact_root: str | Path,
    *,
    profile: str,
    treatment: str = "adaptive",
    metric: str = "makespan_s",
) -> tuple[str, dict[str, Any]]:
    """Render one profile's report. Returns ``(markdown, machine_readable)``."""
    runs = load_runs(artifact_root, profile=profile)
    report = ProfileReport(profile=profile, runs=runs, treatment=treatment)
    admissible, excluded = report.admissible()
    gate_misfires = low_load_dominates(report)
    scope = runs if gate_misfires else admissible
    scoped = ProfileReport(profile, scope, treatment)

    payload = {
        "profile": profile,
        "treatment": treatment,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "runs_total": len(runs),
        "runs_admissible": len(admissible),
        "excluded": excluded,
        # When true, the numbers below are boundary-condition/descriptive per
        # docs/adaptive-v1-preregistration.md section 6.3, not primary evidence
        # for H3. Consumers must not present them as the latter.
        "load_gate_misfires": gate_misfires,
        "scope": "all_runs" if gate_misfires else "admissible_only",
        "evidence_class": "descriptive" if gate_misfires else "primary",
        "versions_by_strategy": {
            strategy: sorted(versions)
            for strategy, (versions, _) in _versions_by_strategy(runs).items()
        },
        "series": {
            metric_name: {k: v.as_dict() for k, v in scoped.series(metric_name).items()}
            for metric_name in REPORTED_METRICS
        },
        "comparisons": [
            c.as_dict()
            for c in compare_all(scoped.series(metric), treatment=treatment, metric=metric)
        ],
        "tie_break_rates": report.tie_break_rates(),
    }
    return render_markdown(report, metric=metric), payload


def profiles_in(artifact_root: str | Path) -> list[str]:
    return sorted({run.profile for run in load_runs(artifact_root)})


def summarise_runs(runs: Iterable[ArchivedRun]) -> list[dict[str, Any]]:
    """One row per run, for a listing endpoint or a dashboard table."""
    return [
        {
            "run_id": run.run_id,
            "profile": run.profile,
            "strategy": run.strategy,
            "algorithm_version": run.algorithm_version,
            "telemetry": run.manifest.get("telemetry"),
            "trial": run.manifest.get("trial"),
            "started_at": run.manifest.get("started_at"),
            "finished_at": run.manifest.get("finished_at"),
            "jobs_submitted": run.submitted,
            "jobs_completed": run.completed,
            "truncated": run.truncated,
            "makespan_s": run.value("makespan_s"),
            "throughput_jobs_s": run.value("throughput_jobs_s"),
            "mean_fleet_utilisation": run.value("mean_fleet_utilisation"),
            "load_regime_ok": (run.value("mean_fleet_utilisation") or 0.0) >= 0.50,
        }
        for run in runs
    ]
