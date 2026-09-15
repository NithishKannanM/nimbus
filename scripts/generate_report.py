#!/usr/bin/env python
"""Regenerate every table in the final analysis, from the archive, in one command.

`docs/experiments.md` §8 lists "researcher degrees of freedom in reporting" as a
threat to validity, and commits to this mitigation:

> Analysis script is fixed and run over all archived raws; every table
> regenerable by one command.

This is that command. It reads `.nimbus/artifacts/benchmarks/` and writes
`docs/results.md`. It does not accept per-profile flags for which strategy wins
or which runs to include, because a script whose output depends on how it was
invoked is not a mitigation for anything.

    python scripts/generate_report.py
    python scripts/generate_report.py --root .nimbus/artifacts/benchmarks --out docs/results.md
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from benchmark.analysis import compare_all  # noqa: E402
from benchmark.cost import monthly_idle_usd, project_run, summarise  # noqa: E402
from benchmark.report import (  # noqa: E402
    ProfileReport,
    load_runs,
    low_load_dominates,
    profiles_in,
)

TREATMENT = "adaptive"
PRIMARY_METRIC = "makespan_s"


def cost_table(runs) -> tuple[list[str], dict]:
    costs = [
        project_run(
            run_id=run.run_id,
            strategy=run.strategy,
            makespan_s=run.value("makespan_s") or 0.0,
            jobs_completed=run.completed,
            decisions=int(run.metrics.get("decisions", run.completed)),
            fleet_size=int(run.manifest.get("workers", 3)),
        )
        for run in runs
        if run.value("makespan_s")
    ]
    if not costs:
        return [], {}

    summary = summarise(costs)
    lines = [
        "## Cost",
        "",
        f"**Projected, not measured.** Prices are us-east-1 on-demand list "
        f"prices retrieved {summary['prices_retrieved']} "
        f"(`docs/cost-model.md` §1). No AWS resources were ever provisioned, so "
        f"there is no bill to validate these against "
        f"(`docs/aws-batch-baseline.md`).",
        "",
        "| strategy | n | median $/1000 jobs | median jobs/$ | median makespan |",
        "|---|---:|---:|---:|---:|",
    ]
    for strategy, row in summary["by_strategy"].items():
        lines.append(
            f"| `{strategy}` | {row['n']} | ${row['median_usd_per_1000_jobs']:.4f} | "
            f"{row['median_jobs_per_usd']:,.0f} | {row['median_makespan_s']:.1f}s |"
        )

    lines += [
        "",
        f"Compute is {summary['min_compute_share']:.1%}\u2013"
        f"{summary['max_compute_share']:.1%} of projected cost per run. At "
        f"benchmark scale a run lasts seconds, so the per-job API charges are a "
        f"large share of a very small total; the project's *overall* spend is "
        f"compute-dominated because the fleet runs for hours between runs, not "
        f"only during them (`docs/cost-model.md` \u00a74).",
        "",
    ]
    if summary["variation_is_makespan"]:
        lines += [
            "> **This table is a restatement of makespan, not an independent "
            "result.** Ranking the strategies by cost reproduces the makespan "
            "ranking exactly \u2014 checked, not assumed. Every strategy runs "
            "the same seeded job sequence on an identical fleet, so the queue "
            "and state charges are near-identical across arms and the term that "
            "actually varies is compute: fleet size \u00d7 hourly price "
            "\u00d7 makespan. `docs/cost-model.md` \u00a75 requires this to be "
            "said rather than letting a table that happens to agree with the "
            "makespan table read as corroboration. It is one measurement "
            "reported twice.",
            "",
        ]

    idle = monthly_idle_usd()
    lines += [
        f"Idle stack, if nobody runs `terraform destroy`: "
        f"**${idle['total_usd']:.2f}/month** "
        f"(${idle['compute_usd']:.2f} compute + ${idle['storage_usd']:.2f} EBS). "
        f"That exceeds the project's entire ~$8 budget in "
        f"**{idle['days_to_exceed_8_usd']:.1f} days**, which is why teardown is "
        f"listed as a non-negotiable cost control rather than as tidy-up.",
        "",
    ]
    return lines, summary


def profile_section(report: ProfileReport) -> tuple[list[str], dict]:
    admissible, excluded = report.admissible()
    misfires = low_load_dominates(report)
    scope = report.runs if misfires else admissible
    scoped = ProfileReport(report.profile, scope, report.treatment)

    comparisons = compare_all(
        scoped.series(PRIMARY_METRIC), treatment=TREATMENT, metric=PRIMARY_METRIC
    )

    lines = [
        f"### Profile `{report.profile}`",
        "",
        f"{len(report.runs)} archived run(s); {len(admissible)} pass the §6.3 "
        f"load-regime gate.",
        "",
    ]

    if misfires:
        lines += [
            "**Evidence class: descriptive.** The load gate excludes most of "
            "this profile's runs, which on an external-pressure profile is the "
            "mis-fire recorded in `docs/adaptive-v1-preregistration.md` §7: a "
            "scheduler that successfully steers work away from a saturated "
            "worker leaves that worker idle *from Nimbus's point of view*, "
            "which lowers measured fleet utilisation. The gate then excludes "
            "exactly the runs where the effect is largest. Figures below are "
            "computed over all runs and are boundary-condition evidence, not "
            "primary evidence for H3.",
            "",
        ]
    elif len(admissible) < len(report.runs) / 2:
        # The gate is doing its job. This profile applies no external pressure,
        # so low fleet utilisation means the runs really were too light -- and
        # in that regime the static baseline degenerates into its `worker_id`
        # tie-break (R9), which is precisely the confound the gate exists to
        # catch. Reporting these numbers as evidence would show adaptive
        # winning for a reason unrelated to telemetry.
        lines += [
            "**Evidence class: inadmissible.** Most runs fall below the §6.3 "
            "load-regime gate, and this profile applies no external pressure — "
            "so this is genuinely low load, not the gate mis-firing. In that "
            "regime the static baseline degenerates into its `worker_id` "
            "tie-break (R9) and any advantage `adaptive` shows would be for a "
            "reason unrelated to telemetry. Figures below cover the admissible "
            "runs only and are reported for completeness, not as evidence.",
            "",
        ]
        if not admissible:
            lines += ["No admissible runs, so no comparison is reported.", ""]
            return lines, {"profile": report.profile, "runs_total": len(report.runs),
                           "runs_admissible": 0, "evidence_class": "inadmissible",
                           "series": {}, "comparisons": [],
                           "tie_break_rates": report.tie_break_rates(),
                           "excluded": excluded}
    else:
        lines += ["**Evidence class: primary.**", ""]

    series = scoped.series(PRIMARY_METRIC)
    if series:
        lines += [
            f"| strategy | n | median {PRIMARY_METRIC} | IQR | range |",
            "|---|---:|---:|---|---|",
        ]
        for name in sorted(series):
            entry = series[name].as_dict()
            lines.append(
                f"| `{name}` | {entry['n']} | {entry['median']:g} | "
                f"{entry['iqr'][0]:g}–{entry['iqr'][1]:g} | "
                f"{entry['range'][0]:g}–{entry['range'][1]:g} |"
            )
        lines.append("")

    if comparisons:
        lines += [
            f"Mann-Whitney U, two-sided, Bonferroni-corrected across "
            f"{len(comparisons)} comparisons (α = {comparisons[0].alpha:.4f}).",
            "",
            "| comparison | median Δ | p | Cliff's δ | effect | significant | "
            "ranges disjoint |",
            "|---|---:|---:|---:|---|---|---|",
        ]
        for c in comparisons:
            lines.append(
                f"| adaptive vs `{c.baseline}` | {c.median_delta_pct:+.1f}% | "
                f"{c.p_value:.4f} | {c.cliffs_delta:+.2f} | {c.effect_label} | "
                f"{'yes' if c.significant else 'no'} | "
                f"{'yes' if c.ranges_disjoint else 'no'} |"
            )
        lines.append("")

    rates = report.tie_break_rates()
    if rates:
        lines += [
            "Placements decided by the `worker_id` tie-break rather than by the "
            "policy's own scoring:",
            "",
            "| strategy | tie-break share |",
            "|---|---:|",
        ]
        for strategy, rate in rates.items():
            lines.append(f"| `{strategy}` | {rate:.1%} |")
        lines += [
            "",
            "Where a static policy ties often and `adaptive` does not, part of "
            "any measured advantage is *not tying* rather than telemetry "
            "content. `fifo` never ties, which makes **adaptive vs `fifo`** the "
            "comparison free of this confound.",
            "",
        ]

    if excluded:
        lines += [
            f"<details><summary>{len(excluded)} run(s) excluded from primary "
            f"evidence</summary>",
            "",
        ]
        lines += [f"- {reason}" for reason in excluded]
        lines += ["", "</details>", ""]

    payload = {
        "profile": report.profile,
        "runs_total": len(report.runs),
        "runs_admissible": len(admissible),
        "evidence_class": (
            "descriptive" if misfires
            else "primary" if len(admissible) >= len(report.runs) / 2
            else "inadmissible"
        ),
        "series": {k: v.as_dict() for k, v in series.items()},
        "comparisons": [c.as_dict() for c in comparisons],
        "tie_break_rates": rates,
        "excluded": excluded,
    }
    return lines, payload


def conclusions(payloads: list[dict]) -> list[str]:
    """Conclusions derived from the tables above, not written alongside them.

    A hand-written conclusions section is the easiest place for a report to
    drift from its own data: the tables regenerate, the prose does not, and a
    claim that was true of an earlier archive silently survives into a version
    that no longer supports it. ``docs/experiments.md`` section 8 lists
    "researcher degrees of freedom in reporting" as a threat to validity; a
    conclusion that cannot contradict its own table is the mitigation.
    """
    primary = [p for p in payloads if p["evidence_class"] == "primary"]
    descriptive = [p for p in payloads if p["evidence_class"] == "descriptive"]
    inadmissible = [p for p in payloads if p["evidence_class"] == "inadmissible"]

    def significant(entries):
        return [
            (p["profile"], c)
            for p in entries
            for c in p["comparisons"]
            if c["significant"]
        ]

    primary_hits = significant(primary)
    descriptive_hits = significant(descriptive)

    out: list[str] = [
        "## Conclusions",
        "",
        "Derived from the tables above by `scripts/generate_report.py`, so they "
        "cannot drift from the data they describe.",
        "",
    ]
    numbered: list[str] = []

    # 1 -- what the primary-evidence profiles actually show.
    if primary and not primary_hits:
        names = ", ".join(f"`{p['profile']}`" for p in primary)
        numbered.append(
            f"**On the profile(s) admissible as primary evidence ({names}), no "
            f"comparison reaches significance.** No strategy beats another on "
            f"makespan. For a homogeneous fleet under saturation that is the "
            f"*predicted* outcome and a good one: "
            f"`docs/adaptive-v1-preregistration.md` \u00a76.4 lists Profile A as "
            f"\"primary \u2014 a null result here is a *good* result\", because "
            f"there is little for any scheduler to exploit and what is under "
            f"test is whether `adaptive` adds overhead or instability when "
            f"telemetry carries no extra information. It does not."
        )
    elif primary_hits:
        detail = "; ".join(
            f"`{profile}`: adaptive vs `{c['baseline']}` "
            f"{c['median_delta_pct']:+.1f}%, p={c['p_value']:.4f}, "
            f"\u03b4={c['cliffs_delta']:+.2f}"
            for profile, c in primary_hits
        )
        numbered.append(f"**Significant differences on primary evidence:** {detail}.")
    else:
        numbered.append(
            "**No profile qualifies as primary evidence.** Every archived "
            "profile is either descriptive or inadmissible under \u00a76.3, so "
            "this run of the analysis supports no primary claim about H3."
        )

    # 2 -- the pressure profiles, correctly labelled.
    if descriptive_hits:
        detail = "; ".join(
            f"`{profile}` vs `{c['baseline']}` {c['median_delta_pct']:+.1f}% "
            f"(p={c['p_value']:.4f}, \u03b4={c['cliffs_delta']:+.2f}, "
            f"{c['effect_label']})"
            for profile, c in descriptive_hits
        )
        numbered.append(
            f"**Under external pressure, `adaptive` reduced makespan "
            f"substantially \u2014 as boundary-condition evidence only.** "
            f"{detail}. Those profiles are classed descriptive because the "
            f"\u00a76.3 load gate excludes their runs, which on a pressure "
            f"profile is the mis-fire recorded in "
            f"`docs/adaptive-v1-preregistration.md` \u00a77 rather than a sign "
            f"the runs were too light. Descriptive evidence is not primary "
            f"evidence, and this is not presented as confirming H3."
        )
    elif descriptive:
        numbered.append(
            "**The external-pressure profiles produced no significant "
            "difference**, even as descriptive evidence."
        )

    # 3 -- the tie-break confound, quantified from the tables.
    rates: dict[str, list[float]] = {}
    for payload in payloads:
        for strategy, rate in payload["tie_break_rates"].items():
            rates.setdefault(strategy, []).append(rate)
    worst_static = max(
        ((s, max(v)) for s, v in rates.items() if s.startswith("static")),
        key=lambda kv: kv[1],
        default=None,
    )
    if worst_static:
        adaptive_max = max(rates.get("adaptive", [0.0]))
        numbered.append(
            f"**Part of any advantage over the static policies is not "
            f"telemetry.** `{worst_static[0]}` decides up to "
            f"{worst_static[1]:.1%} of placements by the `worker_id` tie-break "
            f"rather than by its own scoring, while `adaptive` tops out at "
            f"{adaptive_max:.1%} \u2014 a continuous observed signal rarely "
            f"produces exact equality. `fifo` never ties, which makes "
            f"adaptive-vs-`fifo` the only comparison free of this confound."
        )

    # 4 -- the pre-registered criterion, and whether it fired.
    overlapping = [
        c
        for p in payloads
        for c in p["comparisons"]
        if c["significant"] and not c["ranges_disjoint"]
    ]
    if overlapping:
        numbered.append(
            f"**The pre-registered materiality criterion failed on "
            f"{len(overlapping)} comparison(s) that the standing statistical "
            f"protocol finds significant.** It required non-overlapping per-run "
            f"ranges, which is near-unsatisfiable on a bimodal distribution at "
            f"this N regardless of effect size: one unlucky treatment run and "
            f"one lucky baseline run overlap the ranges while the medians stay "
            f"far apart. That is a flaw in the criterion, not evidence against "
            f"the effect \u2014 but it was fixed in advance, so it is reported "
            f"as failing rather than quietly replaced."
        )

    # 5 -- cost, and what it is not.
    numbered.append(
        "**Cost differences between strategies are makespan in different "
        "units.** All strategies rent an identical fleet, so cost per hour is "
        "identical and only the rental duration varies. Not an independent "
        "result, and not presented as one."
    )

    for index, item in enumerate(numbered, start=1):
        out += [f"{index}. {item}", ""]

    if inadmissible:
        names = ", ".join(f"`{p['profile']}`" for p in inadmissible)
        out += [
            f"Profiles {names} produced no admissible evidence: most of their "
            f"runs fall below the load-regime gate and they apply no external "
            f"pressure, so that is genuine low load rather than the gate "
            f"mis-firing. In that regime the static baseline degenerates into "
            f"its tie-break, and any apparent win would be for the wrong reason.",
            "",
        ]

    out += [
        "## What these results do not support",
        "",
        "- **Nothing about AWS Batch or ECS.** No AWS resources were ever "
        "provisioned. See `docs/aws-batch-baseline.md` for the refusal to "
        "publish an unfair comparison, in the form `docs/experiments.md` "
        "\u00a76.3 requires.",
        "- **Nothing about EC2.** These are local runs. Three processes pinned "
        "to disjoint cores on one host share a page cache and a memory bus in a "
        "way three EC2 instances do not (`docs/limitations.md` \u00a73), so "
        "these numbers are directional and the headline experiment needs "
        "re-running on real hardware.",
        "- **Nothing about regimes that were not run.** Every statement is "
        "bounded by the profiles and load levels in the tables above.",
        "- **Nothing about production scheduling.** Nimbus has no dependency "
        "DAGs, no fair-share, no multi-tenancy, no autoscaling and no "
        "authentication. It is more capable than AWS Batch on exactly two "
        "dimensions \u2014 placement signal and per-decision explainability "
        "\u2014 and those two are the research question.",
        "",
    ]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".nimbus/artifacts/benchmarks")
    parser.add_argument("--out", default="docs/results.md")
    parser.add_argument("--json-out", default="docs/results.json")
    args = parser.parse_args()

    root = Path(args.root)
    all_runs = load_runs(root)
    if not all_runs:
        print(f"no archived runs under {root}", file=sys.stderr)
        return 1

    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        "# Nimbus — results",
        "",
        f"Generated {generated} by `scripts/generate_report.py` from "
        f"`{root}`. Every figure below is derived from the archive; nothing is "
        "typed in by hand. Re-running the command regenerates this file.",
        "",
        f"**{len(all_runs)} archived runs** across "
        f"{len(profiles_in(root))} profiles.",
        "",
        "---",
        "",
        "## Hypothesis",
        "",
        "> For heterogeneous workloads on workers with dynamically changing "
        "resource utilisation, do scheduling decisions based on real-time "
        "telemetry improve completion latency, utilisation and load balance "
        "compared with simpler strategies?",
        "",
        "The real test is not *does adaptive beat FIFO* — it should, almost "
        "arithmetically. It is **does adaptive beat Static Resource-Aware**: "
        "does live telemetry add anything over the reservation accounting ECS "
        "already does.",
        "",
        "## Per-profile results",
        "",
    ]

    payloads = []
    for profile in profiles_in(root):
        report = ProfileReport(profile, load_runs(root, profile=profile), TREATMENT)
        section, payload = profile_section(report)
        lines += section
        if payload:
            payloads.append(payload)

    lines += ["---", ""]
    cost_lines, cost_summary = cost_table(all_runs)
    lines += cost_lines

    lines += ["---", ""] + conclusions(payloads)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))

    Path(args.json_out).write_text(
        json.dumps(
            {
                "generated_at": generated,
                "archive_root": str(root),
                "runs_total": len(all_runs),
                "profiles": payloads,
                "cost": cost_summary,
            },
            indent=2,
        )
    )

    print(f"wrote {out} and {args.json_out} from {len(all_runs)} archived runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
