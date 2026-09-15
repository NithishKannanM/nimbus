"""Report generation.

The constraint under test is that every reported figure is *derived from the
archive*. So these tests build archives on disk and assert on what comes out --
including the awkward cases, which are the ones that matter: a truncated run, a
run below the load gate, a profile where the load gate excludes the entire
experiment, and one strategy appearing under two algorithm versions.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmark.report import (
    ProfileReport,
    build_report,
    load_runs,
    low_load_dominates,
    profiles_in,
    summarise_runs,
)


def write_run(
    root: Path,
    run_id: str,
    *,
    strategy: str,
    profile: str = "cpu_pressure",
    makespan: float = 12.0,
    utilisation: float = 0.7,
    completed: int = 60,
    planned: int = 60,
    version: str = "adaptive-v1",
    started_at: str = "2026-08-01T00:00:00+00:00",
    decisions: list[dict] | None = None,
) -> Path:
    directory = root / run_id
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(
        json.dumps(
            {
                "experiment_id": run_id,
                "strategy": strategy,
                "profile": profile,
                "algorithm_version": version,
                "job_count": planned,
                "telemetry": True,
                "trial": 0,
                "started_at": started_at,
                "finished_at": started_at,
            }
        )
    )
    (directory / "metrics.json").write_text(
        json.dumps(
            {
                "jobs_submitted": planned,
                "jobs_completed": completed,
                "makespan_s": makespan,
                "throughput_jobs_s": completed / makespan if makespan else 0.0,
                "mean_fleet_utilisation": utilisation,
                "completion_ms": {"p50": 100.0, "p95": 400.0},
                "queue_wait_ms": {"p95": 50.0},
                "scheduling_latency_us": {"p95": 25.0},
                "load_balance": {"utilisation_cv": 0.1, "jain_fairness": 0.95},
            }
        )
    )
    if decisions is not None:
        (directory / "decisions.jsonl").write_text(
            "\n".join(json.dumps(d) for d in decisions) + "\n"
        )
    return directory


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    return tmp_path / "benchmarks"


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def test_runs_load_from_an_archive(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive")
    write_run(archive, "exp-2", strategy="fifo")
    runs = load_runs(archive)
    assert {r.run_id for r in runs} == {"exp-1", "exp-2"}


def test_a_partial_archive_is_skipped_not_half_read(archive: Path):
    """A run interrupted mid-archive is not a run. Treating a partial archive
    as data would put half a run's numbers into a comparison."""
    write_run(archive, "exp-1", strategy="adaptive")
    (archive / "exp-broken").mkdir()
    (archive / "exp-broken" / "manifest.json").write_text('{"experiment_id": "exp-broken"}')
    # no metrics.json
    assert [r.run_id for r in load_runs(archive)] == ["exp-1"]


def test_corrupt_json_is_skipped(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive")
    directory = archive / "exp-bad"
    directory.mkdir()
    (directory / "manifest.json").write_text("{not json")
    (directory / "metrics.json").write_text("{}")
    assert [r.run_id for r in load_runs(archive)] == ["exp-1"]


def test_runs_can_be_filtered_by_profile(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive", profile="cpu_pressure")
    write_run(archive, "exp-2", strategy="adaptive", profile="burst")
    assert len(load_runs(archive, profile="burst")) == 1
    assert profiles_in(archive) == ["burst", "cpu_pressure"]


def test_an_empty_archive_yields_nothing(tmp_path: Path):
    assert load_runs(tmp_path / "nothing") == []


# --------------------------------------------------------------------------- #
# admissibility
# --------------------------------------------------------------------------- #
def test_a_truncated_run_is_excluded(archive: Path):
    """It still produces a makespan, which is exactly why it has to be caught:
    three Phase 3 runs silently completed 59 of 60 and reported ordinary
    numbers."""
    write_run(archive, "exp-1", strategy="adaptive", completed=59, planned=60)
    report = ProfileReport("cpu_pressure", load_runs(archive))
    admissible, reasons = report.admissible()
    assert admissible == []
    assert "truncated" in reasons[0]


def test_a_low_load_run_is_excluded(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive", utilisation=0.30)
    admissible, reasons = ProfileReport("cpu_pressure", load_runs(archive)).admissible()
    assert admissible == []
    assert "R9" in reasons[0]


def test_a_healthy_loaded_run_is_admissible(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive", utilisation=0.75)
    admissible, reasons = ProfileReport("cpu_pressure", load_runs(archive)).admissible()
    assert len(admissible) == 1
    assert reasons == []


def test_the_gate_misfire_is_detected_when_it_excludes_the_experiment(archive: Path):
    """When the gate removes most of a profile it is not filtering noise, it is
    filtering the experiment -- the documented mis-fire on pressure profiles."""
    for i in range(8):
        write_run(archive, f"exp-{i}", strategy="adaptive", utilisation=0.30)
    assert low_load_dominates(ProfileReport("cpu_pressure", load_runs(archive)))


def test_the_gate_is_trusted_when_most_runs_pass_it(archive: Path):
    for i in range(8):
        write_run(archive, f"exp-{i}", strategy="adaptive", utilisation=0.80)
    assert not low_load_dominates(ProfileReport("cpu_pressure", load_runs(archive)))


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _sweep(archive: Path, *, utilisation: float = 0.75) -> None:
    adaptive = [11.3, 11.4, 11.5, 11.6, 11.7, 11.75, 12.2, 31.9]
    fifo = [11.4, 12.4, 32.2, 36.4, 38.2, 44.9, 45.9, 46.1]
    for i, value in enumerate(adaptive):
        write_run(archive, f"exp-a{i}", strategy="adaptive", makespan=value,
                  utilisation=utilisation, version="adaptive-v1")
    for i, value in enumerate(fifo):
        write_run(archive, f"exp-f{i}", strategy="fifo", makespan=value,
                  utilisation=utilisation, version="round-robin-v1")


def test_a_report_is_produced_from_the_archive_alone(archive: Path):
    _sweep(archive)
    markdown, payload = build_report(archive, profile="cpu_pressure")

    assert "# Benchmark report" in markdown
    assert "`makespan_s`" in markdown
    assert payload["runs_total"] == 16
    assert payload["runs_admissible"] == 16
    assert payload["evidence_class"] == "primary"


def test_the_report_finds_the_effect(archive: Path):
    _sweep(archive)
    _, payload = build_report(archive, profile="cpu_pressure")
    comparison = next(c for c in payload["comparisons"] if c["baseline"] == "fifo")
    assert comparison["median_delta_pct"] < -50
    assert comparison["significant"]
    assert comparison["effect_label"] == "large"


def test_both_verdicts_appear_when_they_disagree(archive: Path):
    """The Phase 3 situation: the pre-registered criterion fails on overlapping
    ranges while the statistics find a large effect. Publishing only the
    favourable one would be a choice made after seeing the answer."""
    _sweep(archive)
    markdown, payload = build_report(archive, profile="cpu_pressure")
    comparison = next(c for c in payload["comparisons"] if c["baseline"] == "fifo")
    assert comparison["significant"] and not comparison["ranges_disjoint"]
    assert "The two criteria disagree" in markdown


def test_a_misfiring_gate_downgrades_the_evidence_class(archive: Path):
    _sweep(archive, utilisation=0.30)
    markdown, payload = build_report(archive, profile="cpu_pressure")
    assert payload["evidence_class"] == "descriptive"
    assert payload["load_gate_misfires"] is True
    assert payload["scope"] == "all_runs"
    assert "mis-fires on this profile" in markdown
    # And it still produces numbers, rather than an empty report.
    assert payload["comparisons"]


def test_different_strategies_with_different_versions_are_not_flagged(archive: Path):
    """`adaptive-v1` and `round-robin-v1` are different policies, not different
    versions of one policy. Flagging that would cry wolf on every report."""
    _sweep(archive)
    markdown, _ = build_report(archive, profile="cpu_pressure")
    assert "more than one algorithm version" not in markdown


def test_one_strategy_under_two_versions_is_flagged(archive: Path):
    """This one really is uncomparable: a formula change makes the earlier runs
    measurements of a different scheduler (risk R11)."""
    write_run(archive, "exp-1", strategy="adaptive", version="adaptive-v1")
    write_run(archive, "exp-2", strategy="adaptive", version="adaptive-v2")
    markdown, _ = build_report(archive, profile="cpu_pressure")
    assert "more than one algorithm version" in markdown
    assert "adaptive-v1" in markdown and "adaptive-v2" in markdown


def test_the_report_states_what_it_does_not_claim(archive: Path):
    _sweep(archive)
    markdown, _ = build_report(archive, profile="cpu_pressure")
    assert "## What this report does not claim" in markdown
    assert "AWS Batch" in markdown


def test_markdown_and_payload_agree_on_the_run_count(archive: Path):
    _sweep(archive)
    markdown, payload = build_report(archive, profile="cpu_pressure")
    assert f"from {payload['runs_total']} archived run(s)" in markdown


# --------------------------------------------------------------------------- #
# tie-break confound
# --------------------------------------------------------------------------- #
def test_tie_break_rate_is_computed_from_decision_traces(archive: Path):
    """The direct measure of the R9 confound, and the reason adaptive vs fifo
    is the clean comparison."""
    tied = {"selected_worker": "worker-1", "margin": 0.0}
    decided = {"selected_worker": "worker-2", "margin": 0.15}
    write_run(archive, "exp-s", strategy="static_resource_aware",
              decisions=[tied, tied, decided, decided])
    write_run(archive, "exp-a", strategy="adaptive",
              decisions=[decided, decided, decided, decided])

    rates = ProfileReport("cpu_pressure", load_runs(archive)).tie_break_rates()
    assert rates["static_resource_aware"] == 0.5
    assert rates["adaptive"] == 0.0


def test_unplaced_decisions_do_not_count_toward_the_tie_rate(archive: Path):
    write_run(archive, "exp-s", strategy="fifo",
              decisions=[{"selected_worker": None}, {"selected_worker": "w1", "margin": 1.0}])
    rates = ProfileReport("cpu_pressure", load_runs(archive)).tie_break_rates()
    assert rates["fifo"] == 0.0


def test_a_tie_is_detected_from_candidate_scores_when_no_margin_is_recorded(archive: Path):
    decision = {
        "selected_worker": "worker-1",
        "candidates": {
            "worker-1": {"eligible": True, "score": 0.5},
            "worker-2": {"eligible": True, "score": 0.5},
        },
    }
    write_run(archive, "exp-s", strategy="static_resource_aware", decisions=[decision])
    assert ProfileReport("cpu_pressure", load_runs(archive)).tie_break_rates()[
        "static_resource_aware"
    ] == 1.0


def test_runs_without_decision_traces_are_simply_absent(archive: Path):
    write_run(archive, "exp-1", strategy="adaptive")  # no decisions.jsonl
    assert ProfileReport("cpu_pressure", load_runs(archive)).tie_break_rates() == {}


# --------------------------------------------------------------------------- #
# listing
# --------------------------------------------------------------------------- #
def test_summarise_runs_flags_truncation_and_load_regime(archive: Path):
    write_run(archive, "exp-ok", strategy="adaptive", utilisation=0.8)
    write_run(archive, "exp-trunc", strategy="adaptive", completed=59, planned=60)
    write_run(archive, "exp-light", strategy="adaptive", utilisation=0.2)

    rows = {r["run_id"]: r for r in summarise_runs(load_runs(archive))}
    assert rows["exp-ok"]["load_regime_ok"] and not rows["exp-ok"]["truncated"]
    assert rows["exp-trunc"]["truncated"]
    assert not rows["exp-light"]["load_regime_ok"]
