"""Cost projection.

Everything this module produces is a projection from list prices, never a
measurement -- no AWS resources were ever provisioned. The tests here check the
arithmetic and, more importantly, the two guard rails that stop a projection
from being read as a result: the `measured` flag, and the restatement check.
"""

from __future__ import annotations

import pytest

from benchmark.cost import (
    INSTANCE_HOURLY_USD,
    PRICES_RETRIEVED,
    RunCost,
    compute_share,
    is_restatement_of_makespan,
    monthly_idle_usd,
    project_run,
    summarise,
)


def make(strategy: str, makespan: float, *, jobs: int = 60, decisions: int = 60) -> RunCost:
    return project_run(
        run_id=f"exp-{strategy}-{makespan}",
        strategy=strategy,
        makespan_s=makespan,
        jobs_completed=jobs,
        decisions=decisions,
    )


# --------------------------------------------------------------------------- #
# the projection is labelled as one
# --------------------------------------------------------------------------- #
def test_every_projection_carries_the_measured_flag_and_price_date():
    """A field rather than a docstring note, so anything consuming this carries
    the caveat with the number instead of beside it."""
    cost = make("adaptive", 12.0)
    assert cost.measured is False
    assert cost.prices_retrieved == PRICES_RETRIEVED


def test_the_summary_repeats_the_caveat():
    summary = summarise([make("adaptive", 12.0), make("fifo", 14.0)])
    assert summary["measured"] is False
    assert "no bill" in summary["note"].lower() or "not provisioned" in summary["note"].lower()


# --------------------------------------------------------------------------- #
# arithmetic
# --------------------------------------------------------------------------- #
def test_compute_is_fleet_hours_times_the_hourly_price():
    cost = project_run(
        run_id="exp-1", strategy="fifo", makespan_s=3600.0,
        jobs_completed=100, decisions=100, fleet_size=3,
    )
    # 3 workers + 1 control plane, one hour, t3.small.
    assert cost.compute_usd == pytest.approx(4 * INSTANCE_HOURLY_USD["t3.small"], rel=1e-9)


def test_a_longer_makespan_costs_more_on_identical_work():
    assert make("a", 20.0).total_usd > make("a", 10.0).total_usd


def test_cost_per_1000_jobs_scales_the_total():
    # Both fields are rounded for presentation (6 and 8 decimals), so this
    # compares at the coarser of the two rather than pretending to more
    # precision than the stored values carry.
    cost = make("a", 12.0, jobs=60)
    assert cost.usd_per_1000_jobs == pytest.approx(cost.total_usd / 60 * 1000, abs=1e-6)


def test_a_zero_length_run_costs_only_its_api_calls():
    cost = make("a", 0.0)
    assert cost.compute_usd == 0.0
    assert cost.total_usd > 0.0


def test_a_run_with_no_completions_does_not_divide_by_zero():
    cost = project_run(
        run_id="exp-1", strategy="fifo", makespan_s=10.0,
        jobs_completed=0, decisions=0,
    )
    assert cost.usd_per_1000_jobs == 0.0


def test_an_unknown_instance_type_falls_back_rather_than_raising():
    cost = project_run(
        run_id="exp-1", strategy="fifo", makespan_s=3600.0,
        jobs_completed=10, decisions=10, instance_type="not-a-real-type",
    )
    assert cost.compute_usd > 0.0


# --------------------------------------------------------------------------- #
# the restatement guard
# --------------------------------------------------------------------------- #
def test_cost_ranking_matching_makespan_ranking_is_detected():
    """The check that stops a cost table being read as corroboration."""
    costs = [
        make("adaptive", 10.0), make("adaptive", 11.0),
        make("fifo", 20.0), make("fifo", 21.0),
        make("static_resource_aware", 15.0), make("static_resource_aware", 16.0),
    ]
    assert is_restatement_of_makespan(costs)
    assert summarise(costs)["variation_is_makespan"] is True


def test_a_single_strategy_is_not_a_restatement_of_anything():
    assert not is_restatement_of_makespan([make("adaptive", 10.0), make("adaptive", 12.0)])


def test_the_orderings_can_in_principle_disagree():
    """Guard against a check that is true by construction.

    If a strategy ran far fewer jobs, its per-1000-job cost could rank
    differently from its makespan -- and then the cost table *would* be saying
    something new, and must not be dismissed as a restatement.
    """
    costs = [
        make("cheap-but-slow", 40.0, jobs=4000, decisions=4000),
        make("dear-but-fast", 5.0, jobs=2, decisions=2),
    ]
    assert not is_restatement_of_makespan(costs)


def test_compute_share_is_between_zero_and_one():
    for makespan in (0.0, 1.0, 60.0, 3600.0):
        assert 0.0 <= compute_share(make("a", makespan)) <= 1.0


def test_compute_share_rises_with_run_length():
    """At benchmark scale compute is a minority of a tiny total; over an hour it
    dominates. Conflating the two is what the restatement check exists to avoid."""
    assert compute_share(make("a", 3600.0)) > compute_share(make("a", 10.0))


# --------------------------------------------------------------------------- #
# the budget control
# --------------------------------------------------------------------------- #
def test_an_idle_stack_blows_the_project_budget_in_days():
    idle = monthly_idle_usd()
    assert idle["total_usd"] > 8.0
    assert 0 < idle["days_to_exceed_8_usd"] < 30
